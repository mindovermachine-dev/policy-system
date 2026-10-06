"""Regression tests for two silent failure modes in the signing ceremony.

1. A signed merge whose `run_resolve_near_miss` call raises
   `PendingReviewNotFoundError` stored a generic error outcome and logged
   nothing, so "signed, but the review is still there" had no server-side
   trace. It now logs `passkey_signing`/`sign_verify_merge`/`failed` with the
   reason.
2. The companion-browser shell rendered "This approval link is no longer
   valid." for *every* client-side failure, including a browser WebAuthn
   `SecurityError` on an IP-address host. Only a server rejection may say that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from api.test_routes_near_misses import (
    _fake_dependencies,  # pyright: ignore[reportPrivateUsage]
    _record,  # pyright: ignore[reportPrivateUsage]
)
from webauthn.helpers import base64url_to_bytes

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from passkey_signing.test_signing_ceremony import (
    _ACTOR_ISSUER,  # pyright: ignore[reportPrivateUsage]
    _ACTOR_SUBJECT,  # pyright: ignore[reportPrivateUsage]
    _ORIGIN,  # pyright: ignore[reportPrivateUsage]
    _REVIEW_ID,  # pyright: ignore[reportPrivateUsage]
    _RP_ID,  # pyright: ignore[reportPrivateUsage]
    _Authenticator,  # pyright: ignore[reportPrivateUsage]
    _client,  # pyright: ignore[reportPrivateUsage]
    _enroll,  # pyright: ignore[reportPrivateUsage]
    _seed_pending_approval,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.api.dependencies import provide_near_miss_review_dependencies
from ps_service.auth import Principal

if TYPE_CHECKING:
    import pytest
    from fastapi import FastAPI

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import ResolveOutcome


def test_signed_merge_that_finds_no_review_is_logged_server_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> None:
        logged.append(kwargs)

    # detroit-exception: capturing the log call's args IS the spec here (§1.2)
    monkeypatch.setattr("ps_service.passkey_signing.router.emit_log_entry", _capture)

    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator, sign_count=0)
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        principal=Principal(sub=_ACTOR_SUBJECT, iss=_ACTOR_ISSUER),
    )

    def _resolve_nothing(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = (graph, review_id, decision)
        return None

    dependencies, _ = _fake_dependencies((_record(_REVIEW_ID),), resolve=_resolve_nothing)
    cast("FastAPI", client.app).dependency_overrides[provide_near_miss_review_dependencies] = (
        lambda: dependencies
    )

    options = client.post(f"/approvals/{row.id}/sign/options", json={"code": code}).json()
    assertion = authenticator.build_assertion(
        rp_id=_RP_ID,
        origin=_ORIGIN,
        challenge=base64url_to_bytes(options["challenge"]),
        sign_count=1,
    )
    response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "signed"
    assert "error" in response.json()
    failures = [e for e in logged if e.get("action") == "sign_verify_merge"]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failed"
    extra = cast("dict[str, object]", failures[0]["extra"])
    assert extra["pending_approval_id"] == row.id
    assert extra["review_id"] == _REVIEW_ID
    assert _REVIEW_ID in cast("str", extra["reason"])


def test_signed_merge_whose_execution_raises_is_logged_and_still_answers_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-012: only `PendingReviewNotFoundError` used to be caught, so a
    `CompanyMergePersistenceError`, a raw `redis.RedisError` from one of the
    unwrapped reads, a `CompanyMergeConfigurationError` from the graph opener
    or an `IndexError` on an empty merge result escaped instead -- all of them
    *after* `mark_signed` had committed. That left the row `signed` with no
    outcome recorded, emitted no `sign_verify_merge` entry at all, and 500'd
    the caller, which the shell renders as "This approval link is no longer
    valid." -- the exact symptom this issue exists to remove, reached through
    a second door.
    """
    logged: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> None:
        logged.append(kwargs)

    # detroit-exception: capturing the log call's args IS the spec here (§1.2)
    monkeypatch.setattr("ps_service.passkey_signing.router.emit_log_entry", _capture)

    pending_store = FakePendingApprovalStore()
    credential_store = FakeSigningCredentialStore()
    row, code = _seed_pending_approval(pending_store)
    authenticator = _Authenticator()
    _enroll(credential_store, authenticator, sign_count=0)
    client = _client(
        pending_store=pending_store,
        credential_store=credential_store,
        principal=Principal(sub=_ACTOR_SUBJECT, iss=_ACTOR_ISSUER),
    )

    def _resolve_raises(
        graph: GraphHandle, review_id: str, decision: Literal["keep-separate", "merge"]
    ) -> ResolveOutcome | None:
        _ = (graph, review_id, decision)
        raise RuntimeError("falkordb connection reset by peer")

    dependencies, _ = _fake_dependencies((_record(_REVIEW_ID),), resolve=_resolve_raises)
    cast("FastAPI", client.app).dependency_overrides[provide_near_miss_review_dependencies] = (
        lambda: dependencies
    )

    options = client.post(f"/approvals/{row.id}/sign/options", json={"code": code}).json()
    assertion = authenticator.build_assertion(
        rp_id=_RP_ID,
        origin=_ORIGIN,
        challenge=base64url_to_bytes(options["challenge"]),
        sign_count=1,
    )
    response = client.post(
        f"/approvals/{row.id}/sign/verify", json={"code": code, "credential": assertion}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "signed"
    assert "error" in response.json()
    # The outcome is persisted, not merely returned: an operator polling the
    # approval must not see a signed row with nothing recorded against it.
    signed_row = pending_store.get_by_id(row.id)
    assert signed_row is not None
    assert signed_row.outcome is not None
    failures = [e for e in logged if e.get("action") == "sign_verify_merge"]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failed"
    extra = cast("dict[str, object]", failures[0]["extra"])
    assert extra["pending_approval_id"] == row.id
    assert extra["review_id"] == _REVIEW_ID
    assert "falkordb connection reset by peer" in cast("str", extra["reason"])


def test_shell_does_not_claim_approval_when_the_signed_response_carries_an_error() -> None:
    """AC-BI-011: `/sign/verify` answers 200 `{"status": "signed", "error": ...}`
    when the signature was valid but the action it authorised could not run.
    Rendering "Approved." for that body reports a merge that never happened,
    so the success message must be guarded by the body's own `error` key.

    The server's message is deliberately not interpolated into the page:
    `render` assigns `innerHTML`, and an executor-supplied outcome is not
    trusted markup.
    """
    client = _client(
        pending_store=FakePendingApprovalStore(),
        credential_store=FakeSigningCredentialStore(),
    )

    html = client.get("/approvals/anything").text

    assert "result && result.error" in html
    assert "could not" in html.partition("result && result.error")[2].partition("Approved.")[0]
    assert "Approved. You may close this window." in html


def test_shell_only_claims_invalid_link_for_server_rejections() -> None:
    client = _client(
        pending_store=FakePendingApprovalStore(),
        credential_store=FakeSigningCredentialStore(),
    )

    html = client.get("/approvals/anything").text

    # Server rejections are tagged; anything else (a browser WebAuthn error)
    # must not be reported as an invalid link.
    assert "rejected.fromServer = true" in html
    assert "if (error && error.fromServer)" in html
    assert "could not complete the passkey step" in html
    assert "localhost" in html
