"""Generic rejection of `/approvals/{id}/*` failures, with the real reason logged server-side.

Every rejection the signing-ceremony router can produce collapses to the identical
`PendingApprovalInvalidOrExpiredError` (HTTP 404, the fixed "This approval link is no longer
valid." body): an unknown approval id, a wrong or tampered code, an expired or already-consumed
approval, an unknown or wrong-actor credential, a lost consumption race, a malformed WebAuthn
payload, and a WebAuthn library verification failure (bad signature, wrong relying-party id or
origin, non-monotonic sign count). The response never carries the reason, so a caller cannot
learn which condition failed.

`reject` raises that error and logs the reason with `emit_log_entry` (component
`passkey_signing`, outcome `failed`). `reject_on_webauthn_failure` turns any WebAuthn library
exception raised inside a block into a `reject`, so a verification failure is never left to the
application-wide generic 500 handler.
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
    """Log `reason` server-side only, then raise the one generic rejection.

    Handlers call this instead of raising `PendingApprovalInvalidOrExpiredError` directly, so
    the reason for a rejection is always recorded in the process log even though the HTTP
    response never carries it.

    Args:
        action: The calling route's short name (for example `"sign_verify"`); logged as the
            entry's `action`. Must not be empty.
        pending_approval_id: The approval id from the URL path. Never the secret code and
            never WebAuthn credential material.
        reason: A short, stable explanation (for example `"expired"` or `"code_mismatch"`),
            or the `repr` of a library exception. Logged only; never shown to the caller.

    Raises:
        PendingApprovalInvalidOrExpiredError: always; the function never returns.

    Side Effects:
        Emits one log entry (component `passkey_signing`, outcome `failed`) whose `extra`
        holds `pending_approval_id` and `reason`.
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
    """Turn any WebAuthn library failure raised inside the block into the generic rejection.

    Catches `webauthn.helpers.exceptions.WebAuthnException`, the base class of every failure
    the `webauthn` package raises: a mismatched challenge, a bad signature, a wrong relying-party
    id or origin, a non-monotonic sign count, and a malformed credential payload
    (`InvalidJSONStructure`). Other exceptions propagate unchanged.

    Args:
        action: The calling route's short name, passed to `reject`.
        pending_approval_id: The approval id from the URL path, passed to `reject`.

    Yields:
        Control to the caller, which should run exactly one `webauthn.verify_*` call inside
        the block.

    Raises:
        PendingApprovalInvalidOrExpiredError: the block raised a `WebAuthnException`; its
            `repr` is logged server-side by `reject` and never appears in the response.

    Side Effects:
        On a caught exception, emits one failed-outcome log entry via `reject`.
    """
    try:
        yield
    except WebAuthnException as exc:
        reject(action=action, pending_approval_id=pending_approval_id, reason=repr(exc))
