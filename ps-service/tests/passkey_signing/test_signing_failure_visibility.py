"""Regression tests for two silent failure modes in the signing ceremony.

1. A signed merge whose registered executor raises stored a generic error outcome and logged
   nothing, so "signed, but nothing happened" had no server-side trace. It now logs
   `passkey_signing`/`sign_verify_executor`/`failed` with the reason.
2. The companion-browser shell rendered "This approval link is no longer
   valid." for *every* client-side failure, including a browser WebAuthn
   `SecurityError` on an IP-address host. Only a server rejection may say that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from webauthn.helpers import base64url_to_bytes

from passkey_signing._fakes import FakePendingApprovalStore, FakeSigningCredentialStore
from passkey_signing.test_signing_ceremony import (
    _ACTOR_ISSUER,  # pyright: ignore[reportPrivateUsage]
    _ACTOR_SUBJECT,  # pyright: ignore[reportPrivateUsage]
    _ORIGIN,  # pyright: ignore[reportPrivateUsage]
    _RP_ID,  # pyright: ignore[reportPrivateUsage]
    _TOOL_NAME,  # pyright: ignore[reportPrivateUsage]
    _Authenticator,  # pyright: ignore[reportPrivateUsage]
    _client,  # pyright: ignore[reportPrivateUsage]
    _enroll,  # pyright: ignore[reportPrivateUsage]
    _seed_pending_approval,  # pyright: ignore[reportPrivateUsage]
)
from ps_service.auth import Principal
from ps_service.passkey_signing import executors

if TYPE_CHECKING:
    import pytest

    from ps_service.config import ServiceConfig
    from ps_service.passkey_signing.models import PendingApprovalRow


def test_signed_merge_whose_executor_raises_is_logged_and_still_answers_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception from the registered executor escaped *after* `mark_signed` had committed,
    which would leave the row `signed` with no outcome recorded, log nothing, and 500 the
    caller, which the shell renders as "This approval link is no longer valid." -- the same
    symptom as an expired link, reached through a second path. The router now stores a fixed
    error outcome and logs the real reason server-side.
    """
    logged: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> None:
        logged.append(kwargs)

    # detroit-exception: capturing the log call's args IS the spec here
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

    def _executor_raises(_row: PendingApprovalRow, _config: ServiceConfig) -> dict[str, object]:
        message = "falkordb connection reset by peer"
        raise RuntimeError(message)

    executors._EXECUTORS[_TOOL_NAME] = _executor_raises  # pyright: ignore[reportPrivateUsage]

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
    assert "falkordb" not in str(response.json())
    # The outcome is persisted, not merely returned: an operator polling the
    # approval must not see a signed row with nothing recorded against it.
    signed_row = pending_store.get_by_id(row.id)
    assert signed_row is not None
    assert signed_row.outcome is not None
    failures = [e for e in logged if e.get("action") == "sign_verify_executor"]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failed"
    extra = cast("dict[str, object]", failures[0]["extra"])
    assert extra["pending_approval_id"] == row.id
    assert "falkordb connection reset by peer" in cast("str", extra["reason"])


def test_shell_does_not_claim_approval_when_the_signed_response_carries_an_error() -> None:
    """`/sign/verify` answers 200 `{"status": "signed", "error": ...}`
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
