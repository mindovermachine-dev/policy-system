"""Adversarial hardening tests for the companion-browser signing ceremony
(issue #131, PLAN.md §4 Slice 4, CHANGES.md F2/F3).

Reuses Slice 3's own fixtures/helpers verbatim (`test_signing_ceremony.py`,
same test package -- a plain same-package import, no
`--import-mode=importlib` ordering concern, mirroring
`test_enrollment_ceremony.py`'s own documented "same-package import has no
such problem" precedent) rather than rebuilding the real EC P-256 key pair /
CBOR/COSE / WebAuthn-assertion construction machinery a third time.

Every test here proves one of two things PLAN.md §4 Slice 4 asks for:

1. Every adversarial path (unknown id, wrong code, expired, already-signed,
   wrong-actor credential, a lost `mark_signed` CAS race, a tampered
   challenge, a malformed/garbage WebAuthn payload) returns the identical
   generic, non-leaky `PendingApprovalInvalidOrExpiredError` body -- never a
   500, never distinguishable from one another in the *response* -- while
   the real, distinguishing reason is available server-side only, via
   `passkey_signing.error_handlers.reject`'s `emit_log_entry` call
   (`component="passkey_signing"`).
2. The expiry/status guard (`_require_pending_and_unexpired`, Slice 2) runs
   *before* any `webauthn.*` library call, for all four guarded routes
   (`enroll/options`, `enroll/verify`, `sign/options`, `sign/verify`) -- one
   consistent test pattern, not the two separate ones Slices 2/3 each built
   for their own half.
"""

from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

# Same-package reuse of Slice 3's own fixtures/helpers (see module docstring).
from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from passkey_signing.test_signing_ceremony import (
    _ACTOR_ISSUER,  # pyright: ignore[reportPrivateUsage]  -- reused verbatim, mirrors test_signing_ceremony.py's own cross-package-import precedent
    _ACTOR_SUBJECT,  # pyright: ignore[reportPrivateUsage]
    _INVALID_ERROR_CODE,  # pyright: ignore[reportPrivateUsage]
    _ORIGIN,  # pyright: ignore[reportPrivateUsage]
    _REVIEW_ID,  # pyright: ignore[reportPrivateUsage]
    _RP_ID,  # pyright: ignore[reportPrivateUsage]
    _Authenticator,  # pyright: ignore[reportPrivateUsage]
    _client,  # pyright: ignore[reportPrivateUsage]
    _enroll,  # pyright: ignore[reportPrivateUsage]
    _MergeCall,  # pyright: ignore[reportPrivateUsage]
    _seed_pending_approval,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from ps_service.passkey_signing.models import PendingApprovalRow

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_SIGN_VERIFY_ACTION = "sign_verify"
_ENROLL_VERIFY_ACTION = "enroll_verify"


def _sign_once(
    *,
    pending_store: FakePendingApprovalStore,
    credential_store: FakeSigningCredentialStore,
    recorded_calls: list[_MergeCall],
) -> tuple[PendingApprovalRow, str, dict[str, object]]:
    """Seed a pending approval, enroll a credential, and drive one full,
    successful `sign/options` -> `sign/verify` round trip.

    Returns `(row, code, assertion)` -- the same, now-stale `assertion`
    payload a caller could try to replay.
    """
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator, sign_count=0)
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
    return row, code, assertion


def _failed_log_entries(read_lines: ReadLines, *, action: str) -> list[dict[str, object]]:
    """Return every `component="passkey_signing"` `outcome="failed"` entry for `action`.

    Callers must have already called `configure()` (to install the default
    emitter these tests' rejection paths log to) and flushed it before
    calling this.
    """
    all_lines = read_lines(resolve_default_log_path())
    return [
        line
        for line in all_lines
        if line.get("component") == "passkey_signing"
        and line.get("action") == action
        and line.get("outcome") == "failed"
    ]


# --- 1. replay after a successful sign -----------------------------------------


def test_replayed_signature_is_rejected_and_merge_does_not_re_run(
    read_lines: ReadLines,
) -> None:
    """Sign once (Slice 3's happy path), then replay the *exact same*
    `AuthenticationCredential` payload a second time.

    Asserts: a generic, non-leaky rejection (not a 500, no driver/exception
    text, no stack trace in the body); the graph is unchanged by the second
    attempt (no double-merge -- `recorded_calls` stays length 1, matching
    `test_signing_ceremony.py`'s own "no second merge" proof); and the real
    reason ("already_signed") is available server-side, via the
    `passkey_signing` component's own failed-outcome log entry, even though
    the response never carries it.
    """
    emitter = configure()
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    recorded_calls: list[_MergeCall] = []
    row, code, assertion = _sign_once(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )

    replay_response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )

    assert replay_response.status_code == 404
    body = replay_response.json()
    assert body["error"]["code"] == _INVALID_ERROR_CODE
    assert body["error"]["message"] == "This approval link is no longer valid."
    # AC-BI-015: nothing beyond the fixed generic shape -- no exception text,
    # no traceback, no driver detail.
    assert set(body["error"]) == {"code", "message", "failing_stage"}
    assert recorded_calls == [_MergeCall(_REVIEW_ID, "merge")]

    emitter.flush()
    failed_entries = _failed_log_entries(read_lines, action=_SIGN_VERIFY_ACTION)
    assert failed_entries, "expected a failed sign_verify log entry for the replay"
    assert failed_entries[-1]["reason"] == "already_signed"
    assert failed_entries[-1]["pending_approval_id"] == row.id


# --- 2. genuine concurrency: only one signature wins -----------------------------


def test_two_concurrent_sign_verify_calls_race_on_the_same_row_and_only_one_wins() -> None:
    """Two threads race a real, otherwise-valid signature against the same
    pending approval's `.../sign/verify` at (as close as this test can force)
    the same instant.

    This is the direct proof of `PendingApprovalStore.mark_signed`'s atomic
    CAS's actual *concurrency* guarantee -- `test_signing_ceremony.py`'s own
    replay test only proves the sequential case. `FakePendingApprovalStore`
    gained its own internal lock this slice (`_fakes.py`) specifically so it
    genuinely honors the `mark_signed` contract ("only one caller ever sees
    `True`") under real threads, not merely under sequential calls.
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

    barrier = threading.Barrier(2)

    def _attempt(_index: int) -> int:
        barrier.wait()
        response = client.post(
            f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_attempt, range(2)))

    assert sorted(results) == [200, 404]
    # Exactly one merge executed -- not zero, not two.
    assert recorded_calls == [_MergeCall(_REVIEW_ID, "merge")]
    final_row = pending_store.get_by_id(row.id)
    assert final_row is not None
    assert final_row.status == "signed"


# --- 3. viewing never consumes ---------------------------------------------------


def test_summary_called_twice_before_signing_leaves_status_pending_both_times() -> None:
    """AC-BI-012, adversarial-intent confirmation: an attacker (or an
    impatient legitimate user) hitting `/summary` -- the first call that
    actually consumes the opaque `code` (CHANGES.md F2) -- repeatedly before
    ever signing must never itself flip `status`. Slice 2/3 built the
    mechanism (`status` only changes inside `mark_signed`); this is the
    dedicated test proving repeated *viewing* alone can never trigger it.
    """
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    client = _client(pending_store=pending_store, credential_store=credential_store)

    first = client.post(f"/approvals/{row.id}/summary", json={"code": code})
    second = client.post(f"/approvals/{row.id}/summary", json={"code": code})

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "pending"
    assert second.json()["status"] == "pending"
    stored_row = pending_store.get_by_id(row.id)
    assert stored_row is not None
    assert stored_row.status == "pending"


# --- 4. expired vs. already-consumed vs. wrong-code vs. unknown-id: indistinguishable ----


def test_expired_already_consumed_wrong_code_and_unknown_id_are_byte_identical_responses(
    read_lines: ReadLines,
) -> None:
    """AC-BI-015 applied precisely: expiry, already-consumed, a wrong code,
    and an unknown id are four conceptually distinct reasons (PLAN.md §4's
    own prose draws exactly this distinction) -- this test confirms the
    *response* never lets a caller tell them apart, while the server-side
    log (`passkey_signing`/`sign_verify`, `outcome="failed"`) *does* record a
    different `reason` for each, proving the information isn't simply lost,
    only kept out of the response.
    """
    emitter = configure()
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()

    # Case 1: expired (still 'pending', past `expires_at`).
    expired_row, expired_code = _seed_pending_approval(pending_store)
    pending_store._rows_by_id[expired_row.id] = replace(  # pyright: ignore[reportPrivateUsage]
        expired_row, expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )

    # Case 2: already-consumed (a real successful sign, then replayed).
    recorded_calls: list[_MergeCall] = []
    signed_row, signed_code, signed_assertion = _sign_once(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )

    # Case 3: wrong code against a genuinely still-pending row.
    wrong_code_row, _real_code = _seed_pending_approval(pending_store)
    wrong_code = "not-the-real-code-" + "a" * 20

    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        recorded_calls=recorded_calls,
    )
    bogus_assertion = {
        "id": "eA",
        "rawId": "eA",
        "type": "public-key",
        "response": {"clientDataJSON": "", "authenticatorData": "", "signature": ""},
    }

    responses = {
        "expired": client.post(
            f"/approvals/{expired_row.id}/sign/verify",
            json={"code": expired_code, "credential": bogus_assertion},
        ),
        "already_consumed": client.post(
            f"/approvals/{signed_row.id}/sign/verify",
            json={"code": signed_code, "credential": signed_assertion},
        ),
        "wrong_code": client.post(
            f"/approvals/{wrong_code_row.id}/sign/verify",
            json={"code": wrong_code, "credential": bogus_assertion},
        ),
        "unknown_id": client.post(
            "/approvals/00000000-0000-0000-0000-000000000000/sign/verify",
            json={"code": wrong_code, "credential": bogus_assertion},
        ),
    }

    bodies = {name: response.json() for name, response in responses.items()}
    statuses = {name: response.status_code for name, response in responses.items()}

    assert set(statuses.values()) == {404}
    first_body = next(iter(bodies.values()))
    for name, body in bodies.items():
        assert body == first_body, f"{name} response body differs from the others: {body}"

    # But server-side, the four *are* distinguishable.
    expected_reasons = {
        "expired": "expired",
        "already_consumed": "already_signed",
        "wrong_code": "code_mismatch",
        "unknown_id": "unknown_pending_approval_id",
    }
    emitter.flush()
    failed_entries = _failed_log_entries(read_lines, action=_SIGN_VERIFY_ACTION)
    logged_reasons = [entry.get("reason") for entry in failed_entries]
    for reason in expected_reasons.values():
        assert reason in logged_reasons, (
            f"expected reason {reason!r} in server-side log, got {logged_reasons}"
        )


# --- 5. tampered challenge byte ---------------------------------------------------


def _tamper_client_data_challenge(assertion: dict[str, object]) -> dict[str, object]:
    """Return a copy of `assertion` with one character of its encoded
    `clientDataJSON.challenge` flipped -- the signature is left untouched
    (it was computed over the *original* bytes), so the tampered payload is
    still a real, well-formed WebAuthn response; only the challenge itself
    no longer matches what `sign/options` issued.
    """
    tampered = copy.deepcopy(assertion)
    response = cast("dict[str, object]", tampered["response"])
    client_data_json_b64 = cast("str", response["clientDataJSON"])
    client_data = json.loads(base64url_to_bytes(client_data_json_b64))
    original_challenge = cast("str", client_data["challenge"])
    flipped_first_char = "A" if original_challenge[0] != "A" else "B"
    client_data["challenge"] = flipped_first_char + original_challenge[1:]
    response["clientDataJSON"] = bytes_to_base64url(json.dumps(client_data).encode("utf-8"))
    return tampered


def test_a_hand_tampered_challenge_byte_is_rejected_generically_not_as_a_500() -> None:
    """Build a real, correctly-signed `AuthenticationCredential`, then mutate
    one character of `clientDataJSON`'s encoded `challenge` before sending
    it. The `webauthn` library itself rejects the mismatch
    (`InvalidAuthenticationResponse`, confirmed empirically against the
    installed `webauthn==3.0.1`) -- this test proves the shared
    `error_handlers.reject_on_webauthn_failure` layer maps that into the
    same sanitized 404 body every other rejection in this router already
    returns, not the app-wide generic 500 handler Slice 3 left this path
    falling through to.
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

    tampered = _tamper_client_data_challenge(assertion)

    response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": tampered}
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == _INVALID_ERROR_CODE
    assert body["error"]["message"] == "This approval link is no longer valid."
    assert recorded_calls == []


# --- 6. malformed / garbage payloads ----------------------------------------------


def test_malformed_enroll_verify_payload_is_rejected_generically_not_as_a_500(
    read_lines: ReadLines,
) -> None:
    """A syntactically-valid JSON object that is nowhere close to a real
    `RegistrationCredential` (`{"bogus": True}`), posted with the *correct*
    code against a genuinely still-pending row, previously reached the real,
    unmocked `webauthn.verify_registration_response` and raised
    `InvalidJSONStructure` uncaught -- the app-wide generic 500 handler
    caught it, but with a different status/shape than every other rejection
    in this router. Confirms it is now mapped to the identical 404 body, and
    that the real exception is still visible server-side.
    """
    emitter = configure()
    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    client = _client(pending_store=pending_store, credential_store=credential_store)

    response = client.post(
        f"/approvals/{row.id}/enroll/verify",
        json={"code": code, "credential": {"bogus": True}},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == _INVALID_ERROR_CODE
    assert body["error"]["message"] == "This approval link is no longer valid."
    assert not credential_store.has_any_for_actor(
        actor_subject=_ACTOR_SUBJECT, actor_issuer=_ACTOR_ISSUER, rp_id=_RP_ID
    )

    emitter.flush()
    failed_entries = _failed_log_entries(read_lines, action=_ENROLL_VERIFY_ACTION)
    assert failed_entries, "expected a failed enroll_verify log entry"
    reason = cast("str", failed_entries[-1]["reason"])
    assert "InvalidJSONStructure" in reason


def test_malformed_sign_verify_response_substructure_is_rejected_generically_not_as_a_500() -> None:
    """A `sign/verify` payload with a *valid, enrolled* `rawId` (so it passes
    `_resolve_credential_for_signing`'s own AC-BI-005 check) but a
    `response` object missing required WebAuthn fields -- this reaches the
    real, unmocked `webauthn.verify_authentication_response`, which raises
    `InvalidJSONStructure` (confirmed empirically). Proves the same mapping
    applies past the credential-resolution step, not only before it.
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

    garbage_assertion: dict[str, object] = {
        "id": bytes_to_base64url(authenticator.credential_id),
        "rawId": bytes_to_base64url(authenticator.credential_id),
        "type": "public-key",
        "response": {},  # missing clientDataJSON/authenticatorData/signature
    }

    response = client.post(
        f"/approvals/{row.id}/sign/verify",
        json={"code": code, "credential": garbage_assertion},
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == _INVALID_ERROR_CODE
    assert body["error"]["message"] == "This approval link is no longer valid."
    assert recorded_calls == []


# --- 7. one consistent pattern: expiry beats every webauthn.* call, all 4 routes ----


def test_expiry_is_checked_before_any_webauthn_call_on_all_four_guarded_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slices 2/3 each built (and separately tested) this ordering for their
    own half of the ceremony. This is the one, consistent proof that all
    four guarded routes -- `enroll/options`, `enroll/verify`,
    `sign/options`, `sign/verify` -- share it, via the single
    `_require_pending_and_unexpired` call site (`_verify_code`,
    `router.py`): every `webauthn_rp.*` function is monkeypatched to raise
    if it is ever called at all, then all four routes are hit against the
    same expired-but-`'pending'` row.
    """
    configure()

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no webauthn_rp function may be called for an expired approval")

    for name in (
        "build_registration_options",
        "verify_registration",
        "build_authentication_options",
        "verify_authentication",
    ):
        # detroit-exception: raise-if-called trap proving an ordering/non-interaction
        # guarantee (expiry is checked before any webauthn_rp.* call, across all 4
        # guarded routes), not an assert_called interaction check -- final assertions
        # below are on HTTP response state/output, not on this trap's call count.
        # AUDIT.md §2 case 6 / PLAN.md §5 Slice L. Covers all 4 patched names in this
        # loop (single physical monkeypatch.setattr call site).
        monkeypatch.setattr(f"ps_service.passkey_signing.router.{name}", _fail_if_called)

    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator, sign_count=0)
    expired_row = replace(row, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    pending_store._rows_by_id[row.id] = expired_row  # pyright: ignore[reportPrivateUsage]
    client = _client(pending_store=pending_store, credential_store=credential_store)

    bogus_credential = {
        "id": bytes_to_base64url(authenticator.credential_id),
        "rawId": bytes_to_base64url(authenticator.credential_id),
        "type": "public-key",
        "response": {"clientDataJSON": "", "authenticatorData": "", "signature": ""},
    }

    for path, body in (
        ("enroll/options", {"code": code}),
        ("enroll/verify", {"code": code, "credential": bogus_credential}),
        ("sign/options", {"code": code}),
        ("sign/verify", {"code": code, "credential": bogus_credential}),
    ):
        response = client.post(f"/approvals/{row.id}/{path}", json=body)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == _INVALID_ERROR_CODE, path
