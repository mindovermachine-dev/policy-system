"""Shared pieces of the passkey signing ceremony.

Two package-internal helpers are shared with the companion-browser ceremony router
(`passkey_signing.router`): `_require_pending_and_unexpired` is the single expiry/status guard
for the enrollment and signing ceremonies, and `_compute_sign_challenge` derives the expected
WebAuthn signing challenge.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ps_service.api.errors import PendingApprovalInvalidOrExpiredError

if TYPE_CHECKING:
    from ps_service.passkey_signing.models import PendingApprovalRow


def _require_pending_and_unexpired(  # pyright: ignore[reportUnusedFunction] -- called cross-module from `passkey_signing.router`, never from within this file itself
    row: PendingApprovalRow,
) -> None:
    """Reject an already-signed or expired approval before any WebAuthn verification runs.

    A stale, leaked or already-used code must never reach the WebAuthn registration or
    authentication verification, so this guard runs unconditionally first. It raises the same
    error as every other approval failure, so a caller never learns why an approval was rejected.

    Args:
        row: The already-looked-up pending approval whose one-time code has been verified.

    Returns:
        None; returns normally only when the approval is still pending and unexpired.

    Raises:
        PendingApprovalInvalidOrExpiredError: `row.status` is not `"pending"` (already signed)
            or `row.expires_at` has passed.

    Side Effects:
        None.
    """
    if row.status != "pending" or datetime.now(UTC) > row.expires_at:
        raise PendingApprovalInvalidOrExpiredError


def _compute_sign_challenge(  # pyright: ignore[reportUnusedFunction] -- called cross-module from `passkey_signing.router`, never from within this file itself
    row: PendingApprovalRow,
) -> bytes:
    """Compute the WebAuthn signing challenge for an approval from the row's own fields.

    The challenge is not stored: `pending_approvals` has no challenge column, so it is
    recomputed identically at `.../sign/options` and `.../sign/verify` time and cannot drift
    from the request it was minted for. It is the SHA-256 of the canonical JSON (sorted keys,
    compact separators, so key order and whitespace never change the hash) of `tool_name`,
    `normalized_args`, `actor_subject` and `actor_issuer`, followed by the raw `nonce` bytes.
    That binds the signature to the tool call, its arguments and the requesting actor.

    Args:
        row: The pending approval; its `nonce` is the per-row random value.

    Returns:
        The 32-byte SHA-256 digest used as the WebAuthn challenge.

    Side Effects:
        None; pure computation.
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
