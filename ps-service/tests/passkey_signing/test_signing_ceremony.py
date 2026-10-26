"""HTTP tests for the companion-browser signing ceremony.

Mirrors `test_enrollment_ceremony.py`'s style exactly: `TestClient` +
`app.dependency_overrides` supplying fake `PendingApprovalStore`/
`SigningCredentialStore` instances, so request handling is exercised without
a real Postgres. Approvals are `merge-capabilities` rows, the tool a near-miss merge signs; the
executor registered for that tool is replaced for the test by a recorder (`_client`), because this
module's job is the signing-ceremony plumbing, not the merge. The real executor, planner and
gateway run behind the same ceremony in `test_graph_cleanup_signing_to_merge.py`.

The `AuthenticationCredential` payload posted to `.../sign/verify` is a real,
hand-built WebAuthn authentication response -- a real EC P-256 key pair
signs the real `authenticatorData || sha256(clientDataJSON)` bytes with a
real ECDSA (P-256, SHA-256) signature, run through the actual, unmocked
`webauthn.verify_authentication_response` (via `webauthn_rp.verify_authentication`).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from ps_service.api.dependencies import get_principal, provide_pending_approval_store
from ps_service.auth import Principal
from ps_service.config import ServiceConfig
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.executors import (
    _EXECUTORS,  # pyright: ignore[reportPrivateUsage]  -- the test replaces the one registered executor
    _REFUSAL_RECORDERS,  # pyright: ignore[reportPrivateUsage]  -- the test replaces the one registered recorder
    SignerIdentity,
    register_approval_executor,
    register_signing_refusal_recorder,
)
from ps_service.passkey_signing.router import provide_signing_credential_store
from ps_service.passkey_signing.service import (
    _compute_sign_challenge,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    import pytest

    from ps_service.passkey_signing.models import PendingApprovalRow, SigningCredentialRow

# TestClient's default `base_url` is "http://testserver" -- `webauthn_rp.rp_id_and_origin`
# derives `rp_id`/`origin` from the live request's `Host` header, matching
# `test_enrollment_ceremony.py`'s identical constants.
_RP_ID = "testserver"
_ORIGIN = "http://testserver"

_ACTOR_SUBJECT = "user-1"
_ACTOR_ISSUER = "https://issuer.example.com/"
_OTHER_ACTOR_SUBJECT = "user-2"

_TOOL_NAME = "merge-capabilities"
_REVIEW_ID = "review_aaa"
_WINNER_ID = "capability_existing_a"
_LOSER_ID = "capability_incoming_a"

_INVALID_ERROR_CODE = "pending_approval_invalid_or_expired"


def _app_config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


@dataclass
class _MergeCall:
    review_id: str
    decision: Literal["keep-separate", "merge"]


def _scripted_merge_executor(
    recorded_calls: list[_MergeCall] | None,
) -> Callable[[PendingApprovalRow, ServiceConfig], dict[str, object]]:
    """The recorder that stands in for the `merge-capabilities` executor in these tests."""

    def _execute(row: PendingApprovalRow, config: ServiceConfig) -> dict[str, object]:
        del config
        if recorded_calls is not None:
            recorded_calls.append(_MergeCall(str(row.normalized_args["review_id"]), "merge"))
        return {"survivor_id": _WINNER_ID, "absorbed_id": _LOSER_ID, "merged": True}

    return _execute


def _client(
    *,
    pending_store: FakePendingApprovalStore,
    credential_store: FakeSigningCredentialStore,
    recorded_calls: list[_MergeCall] | None = None,
    principal: Principal | None = None,
) -> TestClient:
    """Build the app with fake stores and a recording `merge-capabilities` executor.

    The registry is process-global; `conftest.py` restores it after every test.
    """
    app = create_app(_app_config())
    _EXECUTORS.pop(_TOOL_NAME, None)
    register_approval_executor(_TOOL_NAME, _scripted_merge_executor(recorded_calls))
    app.dependency_overrides[provide_pending_approval_store] = lambda: pending_store
    app.dependency_overrides[provide_signing_credential_store] = lambda: credential_store
    if principal is not None:
        app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


def _seed_pending_approval(
    store: FakePendingApprovalStore, *, actor_subject: str = _ACTOR_SUBJECT
) -> tuple[PendingApprovalRow, str]:
    """Create a fresh, real 'pending' row for a near-miss merge approval (the shape
    `graph_cleanup.service.create_capability_merge_approval` signs when given a `review_id`).
    """
    return store.create_pending_approval(
        tool_name=_TOOL_NAME,
        normalized_args={
            "survivor_id": _WINNER_ID,
            "absorbed_id": _LOSER_ID,
            "acknowledge_governance_change": False,
            "state_digest": "digest",
            "review_id": _REVIEW_ID,
        },
        actor_subject=actor_subject,
        actor_issuer=_ACTOR_ISSUER,
        display_summary={
            "kind": "Capability",
            "incoming_text": "Report the incident to the authority.",
            "nearest_existing_text": "Conduct a risk assessment.",
            "similarity": 0.62,
        },
    )


# --- real WebAuthn authentication ("get") credential construction -----------------

_COSE_KTY = 1
_COSE_ALG = 3
_COSE_CRV = -1
_COSE_X = -2
_COSE_Y = -3
_COSE_KTY_EC2 = 2
_COSE_ALG_ES256 = -7
_COSE_CRV_P256 = 1

_FLAG_USER_PRESENT = 0b0000_0001
_FLAG_USER_VERIFIED = 0b0000_0100
_FLAG_ATTESTED_DATA_INCLUDED = 0b0100_0000
_AAGUID = b"\x00" * 16


def _cose_ec2_public_key(public_key: ec.EllipticCurvePublicKey) -> bytes:
    numbers = public_key.public_numbers()
    x = numbers.x.to_bytes(32, "big")
    y = numbers.y.to_bytes(32, "big")
    return cbor2.dumps(
        {
            _COSE_KTY: _COSE_KTY_EC2,
            _COSE_ALG: _COSE_ALG_ES256,
            _COSE_CRV: _COSE_CRV_P256,
            _COSE_X: x,
            _COSE_Y: y,
        }
    )


@dataclass
class _Authenticator:
    """A real EC P-256 key pair standing in for one enrolled WebAuthn authenticator."""

    credential_id: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    @property
    def cose_public_key(self) -> bytes:
        return _cose_ec2_public_key(self.private_key.public_key())

    def registration_credential(
        self, *, rp_id: str, origin: str, challenge: bytes
    ) -> dict[str, object]:
        """Build a real, verifiable "none"-attestation `RegistrationCredential` for this key.

        Enrolling it stores this authenticator's own credential id and public key, so the same
        authenticator can then sign with `build_assertion`.
        """
        rp_id_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
        flags = bytes([_FLAG_USER_PRESENT | _FLAG_USER_VERIFIED | _FLAG_ATTESTED_DATA_INCLUDED])
        attested_credential_data = (
            _AAGUID
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self.cose_public_key
        )
        auth_data = rp_id_hash + flags + (0).to_bytes(4, "big") + attested_credential_data
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": bytes_to_base64url(challenge),
                "origin": origin,
            }
        ).encode("utf-8")
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation_object),
            },
        }

    def build_assertion(
        self, *, rp_id: str, origin: str, challenge: bytes, sign_count: int
    ) -> dict[str, object]:
        """Build a real, verifiable `AuthenticationCredential` JSON payload."""
        rp_id_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
        flags = bytes([_FLAG_USER_PRESENT | _FLAG_USER_VERIFIED])
        authenticator_data = rp_id_hash + flags + sign_count.to_bytes(4, "big")

        client_data = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": bytes_to_base64url(challenge),
                "origin": origin,
            }
        ).encode("utf-8")
        client_data_hash = hashlib.sha256(client_data).digest()
        signature = self.private_key.sign(
            authenticator_data + client_data_hash, ec.ECDSA(hashes.SHA256())
        )

        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(authenticator_data),
                "signature": bytes_to_base64url(signature),
            },
        }


def _enroll(
    credential_store: FakeSigningCredentialStore,
    authenticator: _Authenticator,
    *,
    actor_subject: str = _ACTOR_SUBJECT,
    sign_count: int = 0,
    rp_id: str = _RP_ID,
) -> SigningCredentialRow:
    return credential_store.create_signing_credential(
        actor_subject=actor_subject,
        actor_issuer=_ACTOR_ISSUER,
        credential_id=authenticator.credential_id,
        public_key=authenticator.cose_public_key,
        sign_count=sign_count,
        rp_id=rp_id,
    )


def _independent_sign_challenge(row: PendingApprovalRow) -> bytes:
    """Hand-compute the expected sign challenge *without* calling production code.

    A from-scratch re-implementation of the documented recipe (canonical JSON of `tool_name`/
    `normalized_args`/`actor_subject`/`actor_issuer`, `sort_keys=True`,
    `separators=(",", ":")`, then the row's own `nonce` bytes appended,
    then `sha256`) -- proves the challenge binding independently of
    `passkey_signing.service._compute_sign_challenge`, not merely that the
    endpoint agrees with itself.
    """
    canonical = json.dumps(
        {
            "tool_name": row.tool_name,
            "normalized_args": row.normalized_args,
            "actor_subject": row.actor_subject,
            "actor_issuer": row.actor_issuer,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical + row.nonce).digest()


# --- tests --------------------------------------------------------------------


def test_valid_signature_executes_the_merge_and_reports_it() -> None:
    """A full signing ceremony, driven
    entirely through the REST surface, actually executes the merge and
    reports it back through the signing response and the stored outcome.
    """
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator, sign_count=0)
    recorded_calls: list[_MergeCall] = []
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
        principal=Principal(sub=_ACTOR_SUBJECT, iss=_ACTOR_ISSUER),
    )

    options_response = client.post(f"/approvals/{row.id}/sign/options", json={"code": code})
    assert options_response.status_code == 200
    options_body = options_response.json()
    assert options_body["rpId"] == _RP_ID
    assert [c["id"] for c in options_body["allowCredentials"]] == [
        bytes_to_base64url(authenticator.credential_id)
    ]
    challenge = base64url_to_bytes(options_body["challenge"])

    # The challenge binding, proven independently of the production challenge function.
    assert challenge == _compute_sign_challenge(row)
    assert challenge == _independent_sign_challenge(row)

    assertion = authenticator.build_assertion(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge, sign_count=1
    )
    verify_response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )

    assert verify_response.status_code == 200
    assert verify_response.json() == {
        "status": "signed",
        "survivor_id": _WINNER_ID,
        "absorbed_id": _LOSER_ID,
        "merged": True,
    }
    assert recorded_calls == [_MergeCall(_REVIEW_ID, "merge")]

    # The outcome is stored on the approval row, where a later status read finds it.
    signed_row = pending_store.get_by_id(row.id)
    assert signed_row is not None
    assert signed_row.outcome == {
        "survivor_id": _WINNER_ID,
        "absorbed_id": _LOSER_ID,
        "merged": True,
    }

    # The authenticator's reported sign_count was persisted.
    updated_credential = credential_store.get_by_credential_id(authenticator.credential_id)
    assert updated_credential is not None
    assert updated_credential.sign_count == 1


def _bogus_assertion(authenticator: _Authenticator) -> dict[str, object]:
    """A syntactically valid assertion shape naming `authenticator`'s credential, unsigned."""
    return {
        "id": bytes_to_base64url(authenticator.credential_id),
        "rawId": bytes_to_base64url(authenticator.credential_id),
        "type": "public-key",
        "response": {"clientDataJSON": "", "authenticatorData": "", "signature": ""},
    }


@dataclass
class _RefusalCall:
    approval_id: str
    signer: SignerIdentity


def _register_refusal_recorder(tool_name: str) -> list[_RefusalCall]:
    """Replace `tool_name`'s signing-refusal recorder with one that records its calls.

    The registry is process-global; `conftest.py` restores it after every test.
    """
    calls: list[_RefusalCall] = []

    def _record(row: PendingApprovalRow, config: ServiceConfig, signer: SignerIdentity) -> None:
        del config
        calls.append(_RefusalCall(row.id, signer))

    _REFUSAL_RECORDERS.pop(tool_name, None)
    register_signing_refusal_recorder(tool_name, _record)
    return calls


_TOOL_WITHOUT_RECORDER = "test-tool-without-refusal-recorder"


def test_another_actors_credential_for_a_tool_without_a_refusal_recorder_is_never_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool with no signing-refusal recorder rejects another actor's credential before any
    WebAuthn verification, proven by making the verification raise if it is reached at all; the
    approval stays pending.

    `configure()` installs the default emitter the rejection logs to (`error_handlers.reject`).
    """
    configure()

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "verify_authentication must not be called for a tool without a refusal recorder"
        )

    # detroit-exception: another actor's credential never reaches verify_authentication
    monkeypatch.setattr("ps_service.passkey_signing.router.verify_authentication", _fail_if_called)

    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = pending_store.create_pending_approval(
        tool_name=_TOOL_WITHOUT_RECORDER,
        normalized_args={"target": "x"},
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        display_summary={"kind": "test"},
    )
    other_authenticator = _Authenticator()
    _enroll(credential_store, other_authenticator, actor_subject=_OTHER_ACTOR_SUBJECT)
    client = _client(pending_store=pending_store, credential_store=credential_store)
    executed: list[str] = []

    def _execute(signed: PendingApprovalRow, config: ServiceConfig) -> dict[str, object]:
        del config
        executed.append(signed.id)
        return {}

    register_approval_executor(_TOOL_WITHOUT_RECORDER, _execute)

    response = client.post(
        f"/approvals/{row.id}/sign/verify",
        json={"code": code, "credential": _bogus_assertion(other_authenticator)},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == _INVALID_ERROR_CODE
    assert executed == []
    stored = pending_store.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "pending"


def test_another_actors_unverifiable_assertion_is_rejected_without_a_refusal_record() -> None:
    """An assertion naming another actor's credential that does not verify is rejected like any
    invalid signature: the refusal recorder is not called and the approval stays pending.
    """
    configure()
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    other_authenticator = _Authenticator()
    _enroll(credential_store, other_authenticator, actor_subject=_OTHER_ACTOR_SUBJECT)
    recorded_calls: list[_MergeCall] = []
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )
    refusals = _register_refusal_recorder(_TOOL_NAME)

    response = client.post(
        f"/approvals/{row.id}/sign/verify",
        json={"code": code, "credential": _bogus_assertion(other_authenticator)},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == _INVALID_ERROR_CODE
    assert refusals == []
    assert recorded_calls == []
    stored = pending_store.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "pending"
    assert stored.outcome is None


def test_another_actors_verified_assertion_voids_the_approval_and_is_recorded() -> None:
    """A valid assertion from another enrolled actor is rejected, but only after it verifies:
    the approval is consumed with a fixed error outcome, the refusal recorder is called once with
    the verified signer, the signer's sign count is not advanced, and the bound actor can no
    longer sign the approval, so the action never runs.
    """
    configure()
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    bound_authenticator = _Authenticator()
    _enroll(credential_store, bound_authenticator)
    other_authenticator = _Authenticator()
    _enroll(credential_store, other_authenticator, actor_subject=_OTHER_ACTOR_SUBJECT)
    recorded_calls: list[_MergeCall] = []
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )
    refusals = _register_refusal_recorder(_TOOL_NAME)
    challenge = _independent_sign_challenge(row)

    response = client.post(
        f"/approvals/{row.id}/sign/verify",
        json={
            "code": code,
            "credential": other_authenticator.build_assertion(
                rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge, sign_count=1
            ),
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == _INVALID_ERROR_CODE
    assert refusals == [_RefusalCall(row.id, SignerIdentity(_OTHER_ACTOR_SUBJECT, _ACTOR_ISSUER))]
    stored = pending_store.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "signed"
    assert stored.outcome == {
        "error": (
            "this approval was signed by someone other than the person it was issued to and can "
            "no longer be used; ask for a new approval"
        )
    }
    other_credential = credential_store.get_by_credential_id(other_authenticator.credential_id)
    assert other_credential is not None
    assert other_credential.sign_count == 0

    bound_response = client.post(
        f"/approvals/{row.id}/sign/verify",
        json={
            "code": code,
            "credential": bound_authenticator.build_assertion(
                rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge, sign_count=1
            ),
        },
    )

    assert bound_response.status_code == 404
    assert recorded_calls == []
    assert len(refusals) == 1


def test_a_refusal_recorder_that_fails_leaves_the_approval_voided_and_the_rejection_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder that raises does not change the response, and the approval stays voided."""
    configure()
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    other_authenticator = _Authenticator()
    _enroll(credential_store, other_authenticator, actor_subject=_OTHER_ACTOR_SUBJECT)
    recorded_calls: list[_MergeCall] = []
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )
    logged: list[dict[str, object]] = []

    def _capture(**entry: object) -> None:
        logged.append(entry)

    # detroit-exception: the log emitter is the process's output boundary
    monkeypatch.setattr("ps_service.passkey_signing.router.emit_log_entry", _capture)

    def _failing(row: PendingApprovalRow, config: ServiceConfig, signer: SignerIdentity) -> None:
        del row, config, signer
        raise RuntimeError("audit host 10.1.2.3 down")

    _REFUSAL_RECORDERS.pop(_TOOL_NAME, None)
    register_signing_refusal_recorder(_TOOL_NAME, _failing)

    response = client.post(
        f"/approvals/{row.id}/sign/verify",
        json={
            "code": code,
            "credential": other_authenticator.build_assertion(
                rp_id=_RP_ID,
                origin=_ORIGIN,
                challenge=_independent_sign_challenge(row),
                sign_count=1,
            ),
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == _INVALID_ERROR_CODE
    assert "10.1.2.3" not in response.text
    stored = pending_store.get_by_id(row.id)
    assert stored is not None
    assert stored.status == "signed"
    assert recorded_calls == []
    assert logged == [
        {
            "component": "passkey_signing",
            "action": "sign_verify_refusal_record",
            "outcome": "failed",
            "extra": {"pending_approval_id": row.id, "reason": "RuntimeError"},
        }
    ]


def test_replaying_sign_verify_after_a_successful_sign_is_rejected_without_a_second_merge() -> None:
    """The atomic single-use mechanism: signing once succeeds; re-submitting
    the exact same (now-stale) request afterwards is rejected with the
    identical generic "no longer valid" error, and no second merge occurs.

    This is the straightforward sequential case only (sign, then sign
    again) -- exhaustive concurrent-race/replay coverage belongs to the hardening tests,
    not this one.

    `configure()` installs the default emitter this
    rejection path now logs to (`error_handlers.reject`).
    """
    configure()
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator, sign_count=0)
    recorded_calls: list[_MergeCall] = []
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )

    options_body = client.post(f"/approvals/{row.id}/sign/options", json={"code": code}).json()
    challenge = base64url_to_bytes(options_body["challenge"])
    assertion = authenticator.build_assertion(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=challenge, sign_count=1
    )

    first_response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )
    assert first_response.status_code == 200
    assert recorded_calls == [_MergeCall(_REVIEW_ID, "merge")]

    second_response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )

    assert second_response.status_code == 404
    assert second_response.json()["error"]["code"] == _INVALID_ERROR_CODE
    # No second merge: the scripted `resolve_review` delegate was not called again.
    assert recorded_calls == [_MergeCall(_REVIEW_ID, "merge")]


def test_sign_options_and_verify_reject_an_expired_approval_without_calling_webauthn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the sign ceremony: an
    expired approval is rejected by `_require_pending_and_unexpired` before
    either `sign/options` or `sign/verify` ever reaches the `webauthn`
    library.

    `configure()` installs the default emitter this
    rejection path now logs to (`error_handlers.reject`).
    """
    configure()

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("webauthn_rp function must not be called for an expired approval")

    # Ordering proof via raise, not assert_called; mirrors enrollment's
    # proof in test_enrollment_ceremony.py, applied to the sign ceremony's own endpoints.
    # detroit-exception: proves sign/options never reaches webauthn for an expired approval
    monkeypatch.setattr(
        "ps_service.passkey_signing.router.build_authentication_options", _fail_if_called
    )
    # detroit-exception: proves sign/verify never reaches webauthn for an expired approval
    monkeypatch.setattr("ps_service.passkey_signing.router.verify_authentication", _fail_if_called)

    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator)
    expired_row = replace(row, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    pending_store._rows_by_id[row.id] = expired_row  # pyright: ignore[reportPrivateUsage]
    client = _client(pending_store=pending_store, credential_store=credential_store)

    options_response = client.post(f"/approvals/{row.id}/sign/options", json={"code": code})
    assert options_response.status_code == 404
    assert options_response.json()["error"]["code"] == _INVALID_ERROR_CODE

    bogus_assertion = {
        "id": bytes_to_base64url(authenticator.credential_id),
        "rawId": bytes_to_base64url(authenticator.credential_id),
        "type": "public-key",
        "response": {"clientDataJSON": "", "authenticatorData": "", "signature": ""},
    }
    verify_response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": bogus_assertion}
    )
    assert verify_response.status_code == 404
    assert verify_response.json()["error"]["code"] == _INVALID_ERROR_CODE


def test_sign_routes_never_carry_the_code_in_their_url_path() -> None:
    """The `.../sign/*` routes never carry the code in their URL path."""
    from ps_service.passkey_signing.router import build_passkey_signing_router

    router = build_passkey_signing_router()

    sign_paths = [
        cast("str", getattr(route, "path", ""))
        for route in router.routes
        if "/sign/" in cast("str", getattr(route, "path", ""))
    ]
    assert sign_paths, "expected at least one registered .../sign/* route"
    for path in sign_paths:
        assert "code" not in path.lower()
        assert path.startswith("/approvals/{pending_approval_id}")
