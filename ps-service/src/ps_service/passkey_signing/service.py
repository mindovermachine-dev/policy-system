"""Shared merge-gating logic for the near-miss "merge" decision.

A merge is not executed directly: it first creates a pending passkey approval that the caller
completes in a companion browser page. This module holds the one implementation that both the MCP
tool (`near_misses_resolve`) and the REST route (`POST /near-misses/{review_id}/resolve`) call,
so the two surfaces can never gate merges differently:

- `create_merge_pending_approval` creates the pending-approval row and its approval link. The
  caller resolves its own verified `(subject, issuer)` identity and passes it in; this module
  never decides whether a caller is authenticated, only what happens once one is.
- `check_pending_approval` reads a caller's own pending approval (MCP `near_misses_check_approval`
  and REST `GET /near-misses/approvals/{pending_approval_id}`). The ownership check (a caller may
  only poll its own approval) lives here once.
- `MergePendingApproval` and `PendingApprovalStatus` are the result shapes.

Two package-internal helpers are shared with the companion-browser ceremony router
(`passkey_signing.router`): `_require_pending_and_unexpired` is the single expiry/status guard
for the enrollment and signing ceremonies, and `_compute_sign_challenge` derives the expected
WebAuthn signing challenge.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ps_service.api.errors import (
    PendingApprovalInvalidOrExpiredError,
    PendingReviewNotFoundError,
)

if TYPE_CHECKING:
    from typing import Literal

    from ps_service.api.near_miss_review_orchestration import NearMissReviewDependencies
    from ps_service.config import ServiceConfig
    from ps_service.passkey_signing.models import PendingApprovalRow
    from ps_service.passkey_signing.store import PendingApprovalStore

_TOOL_NAME = "near_misses_resolve"
_DECISION = "merge"


@dataclass(frozen=True, slots=True)
class MergePendingApproval:
    """What `create_merge_pending_approval` returns to its caller (MCP tool or REST route).

    Attributes:
        pending_approval_id: Id (UUID string) of the new `pending_approvals` row; used to poll
            its status.
        approval_url: `{base_url}/approvals/{id}#{code}` link the approver opens. The one-time
            code is in the URL fragment only, never in the path, so it is not sent to servers
            in request lines.
        expires_at: ISO 8601 timestamp after which the approval can no longer be signed.
    """

    pending_approval_id: str
    approval_url: str
    expires_at: str


def create_merge_pending_approval(
    *,
    review_id: str,
    actor: tuple[str, str],
    base_url: str,
    config: ServiceConfig,
    near_miss_dependencies: NearMissReviewDependencies,
    store: PendingApprovalStore,
) -> MergePendingApproval:
    """Create the pending passkey approval that gates one near-miss "merge" decision.

    Verifies the review still exists, then stores a `pending_approvals` row that captures the
    tool name, the normalized arguments (`review_id`, `decision="merge"`), the requesting actor
    and a display summary of the two candidate texts. No merge is performed: the merge runs only
    after the approver signs the approval with a passkey.

    Business Rules:
        The caller must already have resolved a real, verified identity and refused unauthenticated
        and local-test-bypass callers; `actor` is trusted here.

    Args:
        review_id: Id of the unresolved near-miss review to merge.
        actor: The caller's verified `(subject, issuer)` identity. Recorded as the approval's
            owner; only this identity can poll it.
        base_url: `{scheme}://{host}` prefix for the approval link, derived by the caller from
            its own transport (the MCP request or the REST request).
        config: Resolved service configuration, used to open the graph.
        near_miss_dependencies: Near-miss review dependency bundle. Only its
            `open_single_tenant_graph` and `list_pending_reviews` are used.
        store: Pending-approval store the new row is persisted through.

    Returns:
        A `MergePendingApproval` with the new row's id, its approval link and its expiry.

    Raises:
        PendingReviewNotFoundError: `review_id` is not among the currently unresolved reviews
            (unknown or already resolved). No approval row is created.
        redis.exceptions.RedisError: The graph is unreachable. No approval row is created.
        PendingApprovalPersistenceError: The approval row could not be written to Postgres.

    Side Effects:
        Reads the pending reviews from the graph and inserts one `pending_approvals` row
        (valid for a fixed window, with a fresh nonce and a hashed one-time code). The raw
        code appears only in the returned link.
    """
    graph = near_miss_dependencies.open_single_tenant_graph(config)
    records = near_miss_dependencies.list_pending_reviews(graph)
    record = next((r for r in records if r.id == review_id), None)
    if record is None:
        raise PendingReviewNotFoundError(f"no unresolved PendingReview with id {review_id!r}")

    normalized_args: dict[str, object] = {"review_id": review_id, "decision": _DECISION}
    display_summary: dict[str, object] = {
        "kind": record.kind,
        "incoming_text": record.incoming_text,
        "nearest_existing_text": record.nearest_existing_text,
        "similarity": record.similarity,
    }
    row, code = store.create_pending_approval(
        tool_name=_TOOL_NAME,
        normalized_args=normalized_args,
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary=display_summary,
    )
    approval_url = f"{base_url}/approvals/{row.id}#{code}"
    return MergePendingApproval(
        pending_approval_id=row.id,
        approval_url=approval_url,
        expires_at=row.expires_at.isoformat(),
    )


@dataclass(frozen=True, slots=True)
class PendingApprovalStatus:
    """What `check_pending_approval` returns for a caller's own pending approval.

    Attributes:
        pending_approval_id: Id of the approval row.
        status: `"pending"` (awaiting a signature), `"signed"` (signed, outcome recorded or
            being recorded) or `"expired"` (not signed before `expires_at`).
        review_id: Id of the near-miss review being merged, or `None` if the stored arguments
            lack it.
        decision: The gated decision (`"merge"`), or `None` if the stored arguments lack it.
        winner_id: Id of the surviving node after the merge; `None` until signed and merged.
        loser_id: Id of the node merged away; `None` until signed and merged.
        error: The stored outcome's safe error message when the signature was valid but the
            action it authorised could not be completed. Without it, null `winner_id` and
            `loser_id` could not be told apart from a failure; `None` otherwise.
    """

    pending_approval_id: str
    status: Literal["pending", "expired", "signed"]
    review_id: str | None
    decision: str | None
    winner_id: str | None
    loser_id: str | None
    error: str | None


def _live_status(row: PendingApprovalRow) -> Literal["pending", "expired", "signed"]:
    """Derive the live status; `"expired"` is computed from `expires_at`, never stored."""
    if row.status == "signed":
        return "signed"
    if datetime.now(UTC) > row.expires_at:
        return "expired"
    return "pending"


def _string_or_none(value: object) -> str | None:
    """Narrow an `outcome`/`normalized_args` JSONB field to `str | None`, never raising."""
    return value if isinstance(value, str) else None


def check_pending_approval(
    *,
    pending_approval_id: str,
    actor: tuple[str, str] | None,
    store: PendingApprovalStore,
) -> PendingApprovalStatus | None:
    """Return the status of a pending approval, but only to the caller who requested it.

    Business Rules:
        Three conditions all present as "not found" (`None`) and are deliberately
        indistinguishable, so a caller cannot learn which one applied: no resolved actor
        identity, an unknown `pending_approval_id`, and an id that belongs to a different
        `(actor_subject, actor_issuer)`. The status is derived live, so an unsigned approval
        past its expiry reads `"expired"`. `winner_id`, `loser_id` and `error` are filled only
        once the approval is signed and an outcome is recorded.

    Args:
        pending_approval_id: Id of the approval row to look up.
        actor: The caller's verified `(subject, issuer)` identity, or `None` when no real actor
            could be resolved for this call.
        store: Pending-approval store to read from.

    Returns:
        The caller's own `PendingApprovalStatus`, or `None` if no such approval is visible to
        this caller.

    Raises:
        PendingApprovalPersistenceError: The Postgres lookup failed.

    Side Effects:
        One read of `pending_approvals`; writes nothing.
    """
    if actor is None:
        return None
    row = store.get_by_id(pending_approval_id)
    if row is None:
        return None
    if (row.actor_subject, row.actor_issuer) != actor:
        return None
    status = _live_status(row)
    winner_id: str | None = None
    loser_id: str | None = None
    error: str | None = None
    if status == "signed" and row.outcome is not None:
        winner_id = _string_or_none(row.outcome.get("winner_id"))
        loser_id = _string_or_none(row.outcome.get("loser_id"))
        error = _string_or_none(row.outcome.get("error"))
    return PendingApprovalStatus(
        pending_approval_id=row.id,
        status=status,
        review_id=_string_or_none(row.normalized_args.get("review_id")),
        decision=_string_or_none(row.normalized_args.get("decision")),
        winner_id=winner_id,
        loser_id=loser_id,
        error=error,
    )


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
