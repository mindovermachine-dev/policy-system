"""HTTP tests for the companion-browser enrollment ceremony (issue #131, PLAN.md §3,
CHANGES.md F2/F3, PLAN.md §4 Slice 2).

Mirrors `test_routes_near_misses.py`'s style: `TestClient` +
`app.dependency_overrides` supplying fake `PendingApprovalStore`/
`SigningCredentialStore` instances, so request handling is exercised without
a real Postgres. `FakePendingApprovalStore`/`FakeSigningCredentialStore`
(`_fakes.py`) live in the same test package, so this file imports them
directly (unlike `tests/api/`'s own duplicated copies -- see that file's
module docstring for why *cross*-package imports of this package's fakes
don't reliably resolve under pytest's `--import-mode=importlib`; a
*same*-package import, as here, has no such problem).

The `RegistrationCredential` payload posted to `.../enroll/verify` is a real,
hand-built "none"-attestation WebAuthn registration response -- a real
EC P-256 key pair, a real CBOR-encoded `authData`/`attestationObject`, a real
`clientDataJSON` -- run through the actual, unmocked
`webauthn.verify_registration_response`. "None" attestation carries no
signature over the client data (the whole point of that format), so no
signing step is needed to produce a response the real verifier accepts; every
other field (`rpIdHash`, flags, the CBOR-encoded COSE public key, the
challenge) must still be bit-for-bit correct or verification genuinely fails,
which is exactly what proves this isn't a mocked/hand-waved payload.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import cbor2
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from ps_service.api.dependencies import provide_pending_approval_store
from ps_service.config import ServiceConfig
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.router import (
    build_passkey_signing_router,
    provide_signing_credential_store,
)
from ps_service.passkey_signing.webauthn_rp import enrollment_challenge

if TYPE_CHECKING:
    import pytest

    from ps_service.passkey_signing.models import PendingApprovalRow

# TestClient's default `base_url` is "http://testserver" (starlette's own default) --
# `webauthn_rp.rp_id_and_origin` derives `rp_id`/`origin` from the live request's
# `Host` header, so these constants are what the router will independently compute
# for every request in this file.
_RP_ID = "testserver"
_ORIGIN = "http://testserver"

_ACTOR_SUBJECT = "user-1"
_ACTOR_ISSUER = "https://issuer.example.com/"

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


def _client(
    *, pending_store: FakePendingApprovalStore, credential_store: FakeSigningCredentialStore
) -> TestClient:
    app = create_app(_app_config())
    app.dependency_overrides[provide_pending_approval_store] = lambda: pending_store
    app.dependency_overrides[provide_signing_credential_store] = lambda: credential_store
    return TestClient(app)


def _seed_pending_approval(
    store: FakePendingApprovalStore,
) -> tuple[PendingApprovalRow, str]:
    """Create a fresh, real 'pending' row via the store's own code/nonce generation."""
    return store.create_pending_approval(
        tool_name="near_misses_resolve",
        normalized_args={"review_id": "review_aaa", "decision": "merge"},
        actor_subject=_ACTOR_SUBJECT,
        actor_issuer=_ACTOR_ISSUER,
        display_summary={
            "kind": "Capability",
            "incoming_text": "Report the incident to the authority.",
            "nearest_existing_text": "Conduct a risk assessment.",
            "similarity": 0.62,
        },
    )


def _expire(store: FakePendingApprovalStore, row: PendingApprovalRow) -> PendingApprovalRow:
    """Mutate `row` in `store` to already be past `expires_at` (still `status='pending'`)."""
    expired_row = replace(row, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    store._rows_by_id[row.id] = expired_row  # pyright: ignore[reportPrivateUsage]
    return expired_row


# --- real "none"-attestation WebAuthn registration credential construction --------

_AAGUID = b"\x00" * 16
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


def _build_registration_credential(
    *, rp_id: str, origin: str, challenge: bytes
) -> dict[str, object]:
    """Build a real, verifiable "none"-attestation `RegistrationCredential` JSON payload.

    A fresh EC P-256 key pair backs the credential's public key; "none"
    attestation carries no attestation signature (nothing to sign with the
    private key), so it is discarded once the public key is COSE-encoded.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    credential_id = secrets.token_bytes(16)
    cose_public_key = _cose_ec2_public_key(private_key.public_key())

    rp_id_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
    flags = bytes([_FLAG_USER_PRESENT | _FLAG_USER_VERIFIED | _FLAG_ATTESTED_DATA_INCLUDED])
    sign_count = (0).to_bytes(4, "big")
    attested_credential_data = (
        _AAGUID + len(credential_id).to_bytes(2, "big") + credential_id + cose_public_key
    )
    auth_data = rp_id_hash + flags + sign_count + attested_credential_data
    attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})

    client_data = json.dumps(
        {
            "type": "webauthn.create",
            "challenge": bytes_to_base64url(challenge),
            "origin": origin,
        }
    ).encode("utf-8")

    return {
        "id": bytes_to_base64url(credential_id),
        "rawId": bytes_to_base64url(credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(client_data),
            "attestationObject": bytes_to_base64url(attestation_object),
        },
    }


# --- tests --------------------------------------------------------------------


def test_enrollment_verify_persists_a_credential_for_the_approvals_actor() -> None:
    """PLAN.md §4 Slice 2's own defining test: a full enrollment ceremony,
    driven entirely through the REST surface, ends with a real
    `signing_credentials` row for the approval's actor.
    """
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    client = _client(pending_store=pending_store, credential_store=credential_store)

    summary_response = client.post(f"/approvals/{row.id}/summary", json={"code": code})
    assert summary_response.status_code == 200
    summary_body = summary_response.json()
    assert summary_body["status"] == "pending"
    assert summary_body["needs_enrollment"] is True
    assert summary_body["display_summary"]["kind"] == "Capability"

    options_response = client.post(f"/approvals/{row.id}/enroll/options", json={"code": code})
    assert options_response.status_code == 200
    options_body = options_response.json()
    assert options_body["rp"]["id"] == _RP_ID
    assert options_body["user"]["name"] == _ACTOR_SUBJECT
    assert base64url_to_bytes(options_body["challenge"]) == enrollment_challenge(row)

    credential = _build_registration_credential(
        rp_id=_RP_ID, origin=_ORIGIN, challenge=enrollment_challenge(row)
    )
    verify_response = client.post(
        f"/approvals/{row.id}/enroll/verify", json={"code": code, "credential": credential}
    )

    assert verify_response.status_code == 200
    assert verify_response.json() == {"status": "enrolled"}
    assert credential_store.has_any_for_actor(
        actor_subject=_ACTOR_SUBJECT, actor_issuer=_ACTOR_ISSUER
    )
    enrolled = credential_store.list_for_actor(
        actor_subject=_ACTOR_SUBJECT, actor_issuer=_ACTOR_ISSUER
    )
    assert len(enrolled) == 1
    assert enrolled[0].sign_count == 0

    # A second `/summary` call now reports enrollment already complete.
    second_summary = client.post(f"/approvals/{row.id}/summary", json={"code": code})
    assert second_summary.json()["needs_enrollment"] is False


def test_enroll_options_and_verify_reject_a_wrong_code_with_the_generic_error() -> None:
    """A wrong `code` is rejected identically at every step -- never distinguished
    from any other invalid/expired condition (AC-BI-015).

    Issue #131 Slice 4: every rejection this router produces now also logs
    its real reason server-side (`error_handlers.reject`) -- `configure()`
    installs the default emitter these tests don't otherwise need, mirroring
    `mcp_interface/test_near_miss_tools.py`'s own established precedent.
    """
    configure()
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, _real_code = _seed_pending_approval(pending_store)
    wrong_code = secrets.token_urlsafe(32)
    client = _client(pending_store=pending_store, credential_store=credential_store)

    summary_response = client.post(f"/approvals/{row.id}/summary", json={"code": wrong_code})
    assert summary_response.status_code == 404
    assert summary_response.json()["error"]["code"] == _INVALID_ERROR_CODE

    options_response = client.post(f"/approvals/{row.id}/enroll/options", json={"code": wrong_code})
    assert options_response.status_code == 404
    assert options_response.json()["error"]["code"] == _INVALID_ERROR_CODE

    verify_response = client.post(
        f"/approvals/{row.id}/enroll/verify",
        json={"code": wrong_code, "credential": {"bogus": True}},
    )
    assert verify_response.status_code == 404
    assert verify_response.json()["error"]["code"] == _INVALID_ERROR_CODE
    assert not credential_store.has_any_for_actor(
        actor_subject=_ACTOR_SUBJECT, actor_issuer=_ACTOR_ISSUER
    )


def test_enroll_options_and_verify_reject_an_expired_approval_without_calling_webauthn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHANGES.md F3's own regression test: an expired approval is rejected by
    `_require_pending_and_unexpired` *before* either `enroll/options` or
    `enroll/verify` ever reaches the `webauthn` library -- proven here by
    making the real `webauthn` calls raise if they are ever reached at all.

    Issue #131 Slice 4: `configure()` installs the default emitter this
    rejection path now logs to (`error_handlers.reject`).
    """
    configure()

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("webauthn_rp function must not be called for an expired approval")

    # Ordering proof via raise, not assert_called (AUDIT.md §2 case 6): the raise fires only if
    # `_require_pending_and_unexpired`'s rejection failed to happen before reaching webauthn.
    # detroit-exception: proves enroll/options never reaches webauthn for an expired approval
    monkeypatch.setattr(
        "ps_service.passkey_signing.router.build_registration_options", _fail_if_called
    )
    # detroit-exception: proves enroll/verify never reaches webauthn for an expired approval
    monkeypatch.setattr("ps_service.passkey_signing.router.verify_registration", _fail_if_called)

    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    _expire(pending_store, row)
    client = _client(pending_store=pending_store, credential_store=credential_store)

    options_response = client.post(f"/approvals/{row.id}/enroll/options", json={"code": code})
    assert options_response.status_code == 404
    assert options_response.json()["error"]["code"] == _INVALID_ERROR_CODE

    verify_response = client.post(
        f"/approvals/{row.id}/enroll/verify",
        json={"code": code, "credential": {"bogus": True}},
    )
    assert verify_response.status_code == 404
    assert verify_response.json()["error"]["code"] == _INVALID_ERROR_CODE


def test_get_approval_shell_renders_identically_regardless_of_status() -> None:
    """CHANGES.md F2: the shell is generic -- same 200 markup for a pending
    approval, an expired one, and an id that was never created at all.
    """
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    pending_row, _code = _seed_pending_approval(pending_store)
    expired_row, _code2 = _seed_pending_approval(pending_store)
    _expire(pending_store, expired_row)
    client = _client(pending_store=pending_store, credential_store=credential_store)

    pending_response = client.get(f"/approvals/{pending_row.id}")
    expired_response = client.get(f"/approvals/{expired_row.id}")
    unknown_response = client.get(f"/approvals/{uuid.uuid4()}")

    for response in (pending_response, expired_response, unknown_response):
        assert response.status_code == 200
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["content-type"].startswith("text/html")

    assert pending_response.text == expired_response.text == unknown_response.text
    # AC-BI-015/F2: no `display_summary`/status is ever embedded server-side.
    assert "Capability" not in pending_response.text


def test_code_never_appears_in_any_registered_router_url_path() -> None:
    """CHANGES.md F2's own structural fix: the capability code moved out of every
    URL the server ever sees -- proven here by inspecting every route this
    router actually registers, not just the ones this file happens to call.
    """
    router = build_passkey_signing_router()

    assert router.routes, "expected at least one registered route"
    for route in router.routes:
        path = getattr(route, "path", "")
        assert "code" not in path.lower()
        assert path.startswith("/approvals/{pending_approval_id}")
