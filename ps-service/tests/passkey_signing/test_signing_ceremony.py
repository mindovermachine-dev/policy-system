"""HTTP tests for the companion-browser signing ceremony (issue #131, PLAN.md §3,
CHANGES.md F2/F3, PLAN.md §4 Slice 3).

Mirrors `test_enrollment_ceremony.py`'s style exactly: `TestClient` +
`app.dependency_overrides` supplying fake `PendingApprovalStore`/
`SigningCredentialStore` instances, so request handling is exercised without
a real Postgres. The merge-execution seam
(`NearMissReviewDependencies.resolve_review`) is scripted the same way
`api/test_routes_near_misses.py`'s/`mcp_interface/test_near_miss_tools.py`'s
own merge-path tests already script it -- `run_resolve_near_miss` (the real
production orchestration function) is called for real by this slice's own
router code, for the first time in this whole feature; only the innermost
graph-touching `resolve_review` callable is a scripted fake, exactly
mirroring this codebase's own established layered-fake convention (the one
`falkordb_live`-marked test proving the underlying merge Cypher itself is
correct already exists, `company_merge/test_pending_review_resolve.py`, and
is not re-proven here -- Slice 3's job is the signing-ceremony plumbing, not
the merge query).

`_fake_dependencies`/`_record` are reused verbatim from
`api.test_routes_near_misses` (a cross-package import, not a third copy):
`ps-service/tests/passkey_signing/` sorts alphabetically *after*
`ps-service/tests/api/`, so this import resolves reliably under pytest's
`--import-mode=importlib` the same way `mcp_interface/test_near_miss_tools.py`'s
own identical cross-package import of the same helpers already does (see
that file's own module docstring for the collection-order rule).

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
from api.test_routes_near_misses import (
    _fake_dependencies,  # pyright: ignore[reportPrivateUsage]  -- reused verbatim, mirrors test_near_miss_tools.py's own cross-package-import precedent
    _record,  # pyright: ignore[reportPrivateUsage]  -- same reuse, mirrors `_fake_dependencies`' own justification immediately above
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from ps_service.api.dependencies import (
    get_principal,
    provide_near_miss_review_dependencies,
    provide_pending_approval_store,
)
from ps_service.auth import Principal
from ps_service.company_merge.models import ResolveOutcome
from ps_service.config import ServiceConfig
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.passkey_signing.router import provide_signing_credential_store
from ps_service.passkey_signing.service import (
    _compute_sign_challenge,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from typing import Literal

    import pytest

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.passkey_signing.models import PendingApprovalRow, SigningCredentialRow

# TestClient's default `base_url` is "http://testserver" -- `webauthn_rp.rp_id_and_origin`
# derives `rp_id`/`origin` from the live request's `Host` header, matching
# `test_enrollment_ceremony.py`'s identical constants.
_RP_ID = "testserver"
_ORIGIN = "http://testserver"

_ACTOR_SUBJECT = "user-1"
_ACTOR_ISSUER = "https://issuer.example.com/"
_OTHER_ACTOR_SUBJECT = "user-2"

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


def _client(
    *,
    pending_store: FakePendingApprovalStore,
    credential_store: FakeSigningCredentialStore,
    recorded_calls: list[_MergeCall] | None = None,
    principal: Principal | None = None,
) -> TestClient:
    def _resolve(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = graph
        if recorded_calls is not None:
            recorded_calls.append(_MergeCall(review_id, decision))
        return ResolveOutcome(
            review_id=review_id, decision=decision, winner_id=_WINNER_ID, loser_id=_LOSER_ID
        )

    record = _record(_REVIEW_ID)
    dependencies, _ = _fake_dependencies((record,), resolve=_resolve)

    app = create_app(_app_config())
    app.dependency_overrides[provide_pending_approval_store] = lambda: pending_store
    app.dependency_overrides[provide_signing_credential_store] = lambda: credential_store
    app.dependency_overrides[provide_near_miss_review_dependencies] = lambda: dependencies
    if principal is not None:
        app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


def _seed_pending_approval(
    store: FakePendingApprovalStore, *, actor_subject: str = _ACTOR_SUBJECT
) -> tuple[PendingApprovalRow, str]:
    """Create a fresh, real 'pending' row for a merge approval (mirrors
    `passkey_signing.service.create_merge_pending_approval`'s own shape).
    """
    return store.create_pending_approval(
        tool_name="near_misses_resolve",
        normalized_args={"review_id": _REVIEW_ID, "decision": "merge"},
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

    A from-scratch re-implementation of the documented recipe (PLAN.md §3,
    CHANGES.md, AC-BI-004/AC-BI-011: canonical JSON of `tool_name`/
    `normalized_args`/`actor_subject`/`actor_issuer`, `sort_keys=True`,
    `separators=(",", ":")`, then the row's own `nonce` bytes appended,
    then `sha256`) -- proves AC-BI-004's binding independently of
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
    """PLAN.md §4 Slice 3's own defining test: a full signing ceremony, driven
    entirely through the REST surface, actually executes the merge and
    reports it back through both the signing response and the resumable
    status check.
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

    # AC-BI-004's binding, proven independently of the production challenge function.
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
        "winner_id": _WINNER_ID,
        "loser_id": _LOSER_ID,
    }
    assert recorded_calls == [_MergeCall(_REVIEW_ID, "merge")]

    # AC-BI-010: verifiable via the resumable status check, REST side --
    # the identical `check_pending_approval` function the MCP
    # `near_misses_check_approval` tool also calls (CHANGES.md F1's "one
    # implementation, two callers" rule).
    status_response = client.get(f"/near-misses/approvals/{row.id}")
    assert status_response.status_code == 200
    status_body = status_response.json()
    assert status_body["status"] == "signed"
    assert status_body["winner_id"] == _WINNER_ID
    assert status_body["loser_id"] == _LOSER_ID

    # PLAN.md §3 step (b): the authenticator's reported sign_count was persisted.
    updated_credential = credential_store.get_by_credential_id(authenticator.credential_id)
    assert updated_credential is not None
    assert updated_credential.sign_count == 1


def test_a_different_actors_credential_is_rejected_before_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: a credential belonging to a different actor is rejected even
    if presented against this approval -- and never even reaches
    `verify_authentication_response`, proven by making that call raise if
    it is ever reached at all (mirrors `test_enrollment_ceremony.py`'s
    identical F3 proof technique, applied here to AC-BI-005).
    Issue #131 Slice 4: `configure()` installs the default emitter this
    rejection path now logs to (`error_handlers.reject`).
    """
    configure()

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "verify_authentication must not be called for a wrong-actor credential"
        )

    # Ordering proof via raise, not assert_called (AUDIT.md §2 case 6): the raise fires only if
    # the actor-binding check failed to reject this credential before verification.
    # detroit-exception: wrong-actor credential never reaches verify_authentication (AC-BI-005)
    monkeypatch.setattr("ps_service.passkey_signing.router.verify_authentication", _fail_if_called)

    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store, actor_subject=_ACTOR_SUBJECT)
    other_authenticator = _Authenticator()
    _enroll(credential_store, other_authenticator, actor_subject=_OTHER_ACTOR_SUBJECT)
    recorded_calls: list[_MergeCall] = []
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )

    # A syntactically valid assertion shape naming the *other* actor's
    # credential id -- its signature is never even checked, since the
    # binding check must reject it first.
    bogus_assertion = {
        "id": bytes_to_base64url(other_authenticator.credential_id),
        "rawId": bytes_to_base64url(other_authenticator.credential_id),
        "type": "public-key",
        "response": {"clientDataJSON": "", "authenticatorData": "", "signature": ""},
    }

    response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": bogus_assertion}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == _INVALID_ERROR_CODE
    assert recorded_calls == []


def test_replaying_sign_verify_after_a_successful_sign_is_rejected_without_a_second_merge() -> None:
    """The atomic single-use mechanism: signing once succeeds; re-submitting
    the exact same (now-stale) request afterwards is rejected with the
    identical generic "no longer valid" error, and no second merge occurs.

    This is the straightforward sequential case only (sign, then sign
    again) -- exhaustive concurrent-race/replay coverage is Slice 4's job
    (PLAN.md §4), not this one's.

    Issue #131 Slice 4: `configure()` installs the default emitter this
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
    """CHANGES.md F3's own regression test, applied to the sign ceremony: an
    expired approval is rejected by `_require_pending_and_unexpired` before
    either `sign/options` or `sign/verify` ever reaches the `webauthn`
    library.

    Issue #131 Slice 4: `configure()` installs the default emitter this
    rejection path now logs to (`error_handlers.reject`).
    """
    configure()

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("webauthn_rp function must not be called for an expired approval")

    # Ordering proof via raise, not assert_called (AUDIT.md §2 case 6); mirrors enrollment's F3
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
    """CHANGES.md F2's structural fix, extended to the new `.../sign/*` routes."""
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
