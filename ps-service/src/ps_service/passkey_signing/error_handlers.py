"""Shared, sanitized error-mapping layer for `passkey_signing.router` (issue #131 Slice 4).

Mirrors two existing conventions rather than inventing a third:
`ps_service.api.error_handlers`'s own "one generic body shape, real detail
never in the response" discipline (PLAN.md §4 Slice 4), and
`mcp_interface.mcp_server`'s `_run_mcp_action` `outcome="failed"` logging
convention (`component`/`action`/`outcome`/`extra` via `emit_log_entry`) --
applied here to `passkey_signing.router`'s handlers instead of an MCP tool
body.

Every `/approvals/{id}/*` rejection this router can produce -- an unknown
`pending_approval_id`, a wrong/tampered `code`, an expired approval, an
already-consumed (`status != "pending"`) approval, a wrong-actor or unknown
signing credential, a lost `mark_signed` CAS race, a malformed/garbage
WebAuthn payload, and a genuine WebAuthn library verification failure (bad
signature, wrong RP id/origin, non-monotonic `sign_count`) -- collapses to
the identical `PendingApprovalInvalidOrExpiredError` (HTTP 404, the fixed
"This approval link is no longer valid." body, already registered in
`ps_service.api.error_handlers`, AC-BI-015). `reject` is the single place
that both raises it and records *why*, server-side only, via
`emit_log_entry` -- the response body itself never carries that detail.

Before this module existed (Slice 3), a genuine `webauthn` library
verification failure was left to propagate uncaught to the app-wide generic
`Exception` handler (HTTP 500) -- still a sanitized body, but a different
shape and status than every other rejection this router produces, and with
no server-side record of *why* verification failed.
`reject_on_webauthn_failure` closes that gap.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, NoReturn

from webauthn.helpers.exceptions import WebAuthnException

from ps_service.api.errors import PendingApprovalInvalidOrExpiredError
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["reject", "reject_on_webauthn_failure"]

_COMPONENT = "passkey_signing"


def reject(*, action: str, pending_approval_id: str, reason: str) -> NoReturn:
    """Log `reason` server-side only, then raise the router's one generic rejection.

    Every `/approvals/{id}/*` handler that decides to reject a request calls
    this instead of raising `PendingApprovalInvalidOrExpiredError` directly,
    so the *reason* for the rejection is never simply discarded -- it is
    always available server-side (via the process's own log sink), even
    though the HTTP response never carries it (AC-BI-015).

    Args:
        action: The route's own short name (e.g. `"sign_verify"`), mirrors
            `_run_mcp_action`'s `action` field -- distinct per route.
        pending_approval_id: The `id` path segment (never the secret `code`,
            and never any WebAuthn credential material).
        reason: A short, stable, server-side-only reason (e.g. `"expired"`,
            `"code_mismatch"`) or a `repr(exc)` for a genuine library
            exception -- never surfaced to the caller.

    Raises:
        PendingApprovalInvalidOrExpiredError: always -- this function never
            returns normally.
    """
    emit_log_entry(
        component=_COMPONENT,
        action=action,
        outcome="failed",
        extra={"pending_approval_id": pending_approval_id, "reason": reason},
    )
    raise PendingApprovalInvalidOrExpiredError


@contextmanager
def reject_on_webauthn_failure(*, action: str, pending_approval_id: str) -> Generator[None]:
    """Map any `webauthn` library exception raised inside the block to `reject`.

    Catches `webauthn.helpers.exceptions.WebAuthnException` -- the common base
    class for every failure the `webauthn==3.0.1` package itself raises,
    including a tampered/mismatched challenge, a bad signature, a wrong RP
    id/origin, a non-monotonic `sign_count`, and a malformed/garbage
    credential payload (`InvalidJSONStructure` -- confirmed empirically
    against the installed package: a plain dict missing required keys, or a
    non-JSON string, raises this, never a bare `KeyError`/`TypeError`/
    `json.JSONDecodeError` that could slip past this one `except` clause).

    Args:
        action: The route's own short name, threaded through to `reject`.
        pending_approval_id: The `id` path segment, threaded through to
            `reject`.

    Yields:
        Control, for the caller to run exactly one `webauthn.verify_*`
        call inside.

    Raises:
        PendingApprovalInvalidOrExpiredError: if the wrapped block raised a
            `WebAuthnException` -- the real exception's `repr()` is logged
            server-side only (via `reject`), never in the response.
    """
    try:
        yield
    except WebAuthnException as exc:
        reject(action=action, pending_approval_id=pending_approval_id, reason=repr(exc))
