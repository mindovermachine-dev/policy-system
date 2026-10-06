"""Shared merge-gating logic for `near_misses_resolve`'s merge branch (issue #131).

CHANGES.md F1's fix: the MCP tool (`mcp_server.near_misses_resolve`) and the
REST route (`api.routes.resolve_near_miss`) must call the exact same
pending-approval-creation function, never two parallel gating mechanisms.
`create_merge_pending_approval` is that single implementation; both callers
resolve their own caller identity (`_resolve_signing_actor()` on the MCP
side, the `get_principal` dependency on the REST side) and pass it in as an
already-verified `(sub, iss)` pair -- this module never itself decides
whether a caller is authenticated, only what happens once one is.

`check_pending_approval` is the analogous single implementation backing both
`near_misses_check_approval` (MCP) and `GET /near-misses/approvals/{id}`
(REST) -- PLAN.md §2.3's ownership check (a caller may only poll their own
pending approval) lives here exactly once.

`_require_pending_and_unexpired` (issue #131 Slice 2, CHANGES.md F3) is the
shared expiry/status guard for the companion-browser ceremony
(`passkey_signing.router`): called at the top of `enroll/options` and
`enroll/verify` in this slice, and (unmodified) `sign/options`/`sign/verify`
from Slice 3 onward -- one implementation, four call sites, so the two
ceremonies can never drift apart on this check again (the exact regression
Critique/Repair flagged against an earlier draft of this plan).
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
    """What `create_merge_pending_approval` returns to its caller (MCP tool or REST route)."""

    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601, `PendingApprovalRow.expires_at.isoformat()`


def create_merge_pending_approval(
    *,
    review_id: str,
    actor: tuple[str, str],
    base_url: str,
    config: ServiceConfig,
    near_miss_dependencies: NearMissReviewDependencies,
    store: PendingApprovalStore,
) -> MergePendingApproval:
    """Create a `pending_approvals` row for one `near_misses_resolve(..., "merge")` call.

    PLAN.md §2.2 steps 2-4 (step 1 -- resolving `actor` and refusing an
    unauthenticated/bypass caller -- is each caller's own responsibility,
    before this function is ever called; `actor` here is always already a
    real, verified `(sub, iss)` pair). Never calls
    `near_miss_review_orchestration.run_resolve_near_miss` -- no merge write
    happens here, only the pending-approval row.

    Args:
        review_id: The `PendingReview` id the caller wants to merge.
        actor: The caller's verified `(sub, iss)` identity, already resolved.
        base_url: `{scheme}://{host}` for the approval link (CHANGES.md F2),
            derived by the caller from its own transport (the MCP `Context`'s
            request, or the REST `Request`).
        config: The resolved service configuration (for opening the graph).
        near_miss_dependencies: The near-miss review dependency bundle (used
            only for its `open_single_tenant_graph`/`list_pending_reviews`
            -- `resolve_review` is never called here).
        store: The `PendingApprovalStore` to persist the new row through.

    Returns:
        A :class:`MergePendingApproval` naming the new row's id, its
        `{base_url}/approvals/{id}#{code}` link (CHANGES.md F2 -- the code
        sits only in the URL fragment, never the path), and its expiry.

    Raises:
        PendingReviewNotFoundError: `review_id` is not among the currently
            unresolved `PendingReview`s (PLAN.md §2.2 step 2's early check --
            the same condition/message `keep-separate`'s `run_resolve_near_miss`
            already raises for a missing/already-resolved review). No
            Postgres row is created on this path.
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
    """What `check_pending_approval` returns for a caller's own pending approval."""

    pending_approval_id: str
    status: Literal["pending", "expired", "signed"]
    review_id: str | None
    decision: str | None
    winner_id: str | None
    loser_id: str | None
    error: str | None
    """The stored outcome's safe error message when the signature was valid but
    the action it authorised could not be completed (AC-BI-013). Without it,
    null `winner_id`/`loser_id` could not be told apart from a failure."""


def _live_status(row: PendingApprovalRow) -> Literal["pending", "expired", "signed"]:
    """Derive status live -- `"expired"` is never itself stored (PLAN.md §2.3, AC-BI-014)."""
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
    """Look up `pending_approval_id`, enforcing PLAN.md §2.3's ownership check.

    Returns `None` for every condition that must present as a generic
    "not found" to the caller: an unresolved/absent actor identity (no
    caller with no real actor can ever poll any pending approval, mirroring
    AC-BI-002's fail-closed rule extended to this read side), an unknown
    `pending_approval_id`, and a `pending_approval_id` that exists but
    belongs to a different `(actor_subject, actor_issuer)` -- deliberately
    never distinguished from "unknown id" in the return value, so neither
    MCP nor REST caller can leak which case applies (AC-BI-015's
    leak-nothing rule, applied here defensively too).

    Args:
        pending_approval_id: The row id to look up.
        actor: The caller's verified `(sub, iss)` identity, or `None` when
            no real actor could be resolved for this call.
        store: The `PendingApprovalStore` to read from.

    Returns:
        The caller's own :class:`PendingApprovalStatus`, or `None` if no
        such approval is visible to this caller.
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


def _require_pending_and_unexpired(  # pyright: ignore[reportUnusedFunction] -- called cross-module from `passkey_signing.router` (`enroll/*` this slice, `sign/*` from Slice 3), never from within this file itself
    row: PendingApprovalRow,
) -> None:
    """Reject an expired/consumed approval before any WebAuthn library call (CHANGES.md F3).

    A stale/leaked/already-consumed `code` must never reach
    `webauthn.verify_registration_response`/`verify_authentication_response`
    at all -- checked here, unconditionally, before that call, not left to
    fall out of some other side effect. Raises the identical
    `PendingApprovalInvalidOrExpiredError` every other `/approvals/{id}/*`
    failure mode raises (AC-BI-015: a caller must never learn *why* an
    approval was rejected).

    Args:
        row: The already-looked-up, already-code-verified pending approval.

    Raises:
        PendingApprovalInvalidOrExpiredError: `row.status != "pending"` (already
            signed) or `row.expires_at` has passed.
    """
    if row.status != "pending" or datetime.now(UTC) > row.expires_at:
        raise PendingApprovalInvalidOrExpiredError


def _compute_sign_challenge(  # pyright: ignore[reportUnusedFunction] -- called cross-module from `passkey_signing.router` (Slice 3), never from within this file itself
    row: PendingApprovalRow,
) -> bytes:
    """Recompute the expected WebAuthn signing challenge fresh from the row's own fields.

    Issue #131 Slice 3 (PLAN.md §3, AC-BI-004/AC-BI-011): never reads back a
    separately stored `challenge` column -- `pending_approvals` has none
    (PLAN.md §1.1) -- so the challenge cannot drift from the request it was
    minted for. Recomputed identically at both `.../sign/options` and
    `.../sign/verify` time from the row's own already-persisted `tool_name`,
    `normalized_args`, `actor_subject`, `actor_issuer`, and `nonce`: the
    first four are canonical-JSON-encoded (`json.dumps(..., sort_keys=True,
    separators=(",", ":"))`, so key order/whitespace can never make two
    logically-identical payloads hash differently), then `nonce`'s raw bytes
    are appended before hashing with `hashlib.sha256` -- binding the
    challenge to the tool call, its arguments, and the actor who requested
    it, exactly as AC-BI-004 requires.
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
