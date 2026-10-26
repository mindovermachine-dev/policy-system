"""The approval ceremony after an actor's signing passkeys were revoked.

Revocation runs through the real `revoke_actor_signing` on the in-memory stores the ceremony's
REST routes also read, and every assertion and registration is real WebAuthn, verified by the
unmocked library. Covered: an approval voided by the revocation can no longer enrol, sign or
execute; a new approval asks for enrolment again; a new device enrols and signs it; the revoked
device's assertion is rejected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from audit._fakes import InMemoryAuditStore
from webauthn.helpers import base64url_to_bytes

from passkey_signing._fakes import (
    FakePendingApprovalStore,
    FakeSigningCredentialStore,
    FakeSigningRevocationStore,
)
from passkey_signing._harness import (
    ACTOR_ISSUER,
    ACTOR_SUBJECT,
    INVALID_ERROR_CODE,
    ORIGIN,
    RP_ID,
    Authenticator,
    MergeCall,
    client,
    enroll,
    seed_pending_approval,
    sign_challenge,
)
from ps_service.logging import configure
from ps_service.passkey_signing.revocation import revoke_actor_signing
from ps_service.passkey_signing.webauthn_rp import enrollment_challenge

if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx
    from fastapi.testclient import TestClient

    from ps_service.passkey_signing.models import PendingApprovalRow


class _Ceremony:
    """One actor with an enrolled old device, the stores, and a client over them."""

    def __init__(self) -> None:
        configure()
        self.pending = FakePendingApprovalStore()
        self.credentials = FakeSigningCredentialStore()
        self.calls: list[MergeCall] = []
        self.old_device = Authenticator()
        enroll(self.credentials, self.old_device)
        self.client: TestClient = client(
            pending_store=self.pending,
            credential_store=self.credentials,
            recorded_calls=self.calls,
        )

    def revoke(self) -> tuple[str, ...]:
        result = revoke_actor_signing(
            actor_subject=ACTOR_SUBJECT,
            actor_issuer=ACTOR_ISSUER,
            revocation_store=FakeSigningRevocationStore(
                credentials=self.credentials, pending=self.pending
            ),
            audit_store=InMemoryAuditStore(),
        )
        return result.voided_approval_ids

    def post(
        self, row: PendingApprovalRow, step: str, body: Mapping[str, object]
    ) -> httpx.Response:
        return self.client.post(f"/approvals/{row.id}/{step}", json=body)  # pyright: ignore[reportReturnType]  # httpx double-build in dep tree: httpx2._models.Response vs httpx._models.Response

    def sign(
        self, row: PendingApprovalRow, code: str, device: Authenticator, sign_count: int = 1
    ) -> httpx.Response:
        assertion = device.build_assertion(
            rp_id=RP_ID, origin=ORIGIN, challenge=sign_challenge(row), sign_count=sign_count
        )
        return self.post(row, "sign/verify", {"code": code, "credential": assertion})


def _assert_generic_rejection(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == INVALID_ERROR_CODE


def test_a_voided_approval_code_can_no_longer_enrol_sign_or_execute() -> None:
    ceremony = _Ceremony()
    row, code = seed_pending_approval(ceremony.pending)
    assert ceremony.revoke() == (row.id,)
    new_device = Authenticator()
    enroll(ceremony.credentials, new_device)
    registration = Authenticator().registration_credential(
        rp_id=RP_ID, origin=ORIGIN, challenge=enrollment_challenge(row)
    )

    for step, body in (
        ("summary", {"code": code}),
        ("enroll/options", {"code": code}),
        ("enroll/verify", {"code": code, "credential": registration}),
        ("sign/options", {"code": code}),
    ):
        _assert_generic_rejection(ceremony.post(row, step, body))
    _assert_generic_rejection(ceremony.sign(row, code, new_device))

    assert ceremony.calls == []
    assert [c.credential_id for c in ceremony.credentials.all_rows()] == [new_device.credential_id]
    voided = ceremony.pending.get_by_id(row.id)
    assert voided is not None
    assert voided.status == "signed"
    assert voided.outcome is not None
    assert "ask for a new approval" in str(voided.outcome["error"])


def test_after_revoke_a_new_approval_reports_needs_enrollment_true() -> None:
    ceremony = _Ceremony()
    ceremony.revoke()
    row, code = seed_pending_approval(ceremony.pending)

    response = ceremony.post(row, "summary", {"code": code})

    assert response.status_code == 200, response.text
    assert response.json()["needs_enrollment"] is True


def test_after_revoke_a_new_device_enrols_and_signs_the_new_approval() -> None:
    ceremony = _Ceremony()
    ceremony.revoke()
    row, code = seed_pending_approval(ceremony.pending)
    new_device = Authenticator()

    options = ceremony.post(row, "enroll/options", {"code": code})
    assert options.status_code == 200, options.text
    registration = new_device.registration_credential(
        rp_id=RP_ID, origin=ORIGIN, challenge=base64url_to_bytes(options.json()["challenge"])
    )
    enrolled = ceremony.post(row, "enroll/verify", {"code": code, "credential": registration})
    assert enrolled.json() == {"status": "enrolled"}
    sign_options = ceremony.post(row, "sign/options", {"code": code})
    assert sign_options.status_code == 200, sign_options.text
    assert len(sign_options.json()["allowCredentials"]) == 1

    signed = ceremony.sign(row, code, new_device)

    assert signed.status_code == 200, signed.text
    assert signed.json()["status"] == "signed"
    assert len(ceremony.calls) == 1


def test_after_revoke_an_assertion_from_the_old_credential_is_rejected() -> None:
    ceremony = _Ceremony()
    ceremony.revoke()
    row, code = seed_pending_approval(ceremony.pending)
    new_device = Authenticator()
    enroll(ceremony.credentials, new_device)

    _assert_generic_rejection(ceremony.sign(row, code, ceremony.old_device))

    assert ceremony.calls == []
    still_pending = ceremony.pending.get_by_id(row.id)
    assert still_pending is not None
    assert still_pending.status == "pending"
