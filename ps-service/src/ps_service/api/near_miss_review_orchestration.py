"""REST-boundary glue for the near-miss review workflow (issue #35, PLAN.md §3.1).

Mirrors `restore_orchestration.py`'s/`ingestion_orchestration.py`'s shape: an
injection seam (:class:`NearMissReviewDependencies`) and
:func:`build_default_near_miss_review_dependencies`, which wires the real
`ps_service.company_merge.pending_review` functions via **function-local**
imports so that importing `ps_service.main` never transitively loads
`ps_service.company_merge` at module load (M6 / the Process Harness
decoupling guarantee).

CHANGES.md M1 (repair resolution, supersedes PLAN.md's original
`open_db`/`single_tenant_graph_name` pair): `RestoreDependencies`
(`restore_orchestration.py:87-92`) was confirmed *not* the right template --
it hands the route a raw `FalkorDB` connection plus a graph name, with graph
selection happening somewhere else not shown in that file. The actually
correct, closer precedent is `ingestion_orchestration.py:1012-1020`'s
`_open_single_tenant_graph(config) -> GraphHandle`, which already does the
full `connect_from_config` -> `select_graph` -> named single-tenant graph
round trip. `_open_single_tenant_graph` below is a byte-for-byte copy of
that function (own private copy, not a shared import -- mirrors
`falkordb_client.py`/`graph_writer.py`'s own "own copy, not shared import"
module-docstring convention, and `pending_review.py`'s own `_execute_query`
precedent).

Slice 3 (AC-BI-004, the not-found half of AC-BI-008, the `keep-separate`
half of AC-BI-009) adds `resolve_review` to this bundle and
`run_resolve_near_miss`, the `POST /near-misses/{review_id}/resolve` entry
point. `pending_review.resolve_review` returns `None` (not a raised
exception) when `review_id` doesn't exist or was already resolved -- this
module is where that `None` is translated into the API-boundary
`PendingReviewNotFoundError` (`api/errors.py`), exactly like
`RestoreArtifactRejectedError`'s own "API-boundary translation of a
lower-layer condition" pattern: `ps_service.company_merge` never imports
from `ps_service.api` (M6 layering is one-directional).

Slice 4 (AC-BI-005/006/007, AC-BI-008/009 completed) widens both to add
`decision="merge"`. `pending_review.resolve_review` raises
`company_merge.errors.StalePendingReviewError` (not `None`) when the merge
branch's existence check (CHANGES.md H2) finds `incoming_id` or
`nearest_existing_id` no longer resolves to a real node -- this module
catches that specific, lightweight, dependency-free exception type
(`ps_service.company_merge.errors` is safe to import at module level here,
mirroring `restore_orchestration.py`'s own `ps_service.restore.errors`
precedent -- unlike `pending_review.py` itself, it is never function-local)
and translates it into `PendingReviewNotFoundError` with H2's dedicated
stale-reference message, before any merge write has happened.

issue #163 Slice D narrowed :func:`build_default_near_miss_review_dependencies`'s
own DI seam, mirroring `ingestion_orchestration.build_default_pipeline_dependencies`'s
own Slice C narrowing exactly: `open_single_tenant_graph` (the true FalkorDB
leaf boundary) is the *only* substitutable parameter now -- `list_pending_reviews`/
`resolve_review` (real Company Merge business logic) are always the real,
shipped `pending_review` functions, with no way for a caller to substitute
fakes for them through this factory's own signature any more. Before this
change, MCP-layer tests replaced this factory's entire return value
wholesale, faking real Company Merge dedup-resolution logic in the name of
substituting only the FalkorDB boundary beneath it -- the same
"factory bundles boundary+business-logic together" violation
`mcp_interface_part1.md`'s/`mcp_interface_part2.md`'s DOMINANT FINDING
flagged repo-wide (`.orchestrator/tracker/issue-163/AUDIT_RAW/`). See
:func:`build_default_near_miss_review_graph_opener` below, the new
approved-mock-boundary entry (`docs/coding-standards/approved-mock-boundaries.yaml`)
tests substitute instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import redis.exceptions

from ps_service.api.errors import PendingReviewNotFoundError
from ps_service.api.models import (
    PendingReviewEntry,
    PendingReviewListResponse,
    ResolveReviewResponse,
)
from ps_service.api.near_miss_audit_actions import (
    NEAR_MISS_RESOLVE_ACTION,
    NEAR_MISS_REVIEW_RESOURCE_TYPE,
)
from ps_service.audit import AuditTarget, record_follow_up_row, record_opening_row
from ps_service.company_merge.errors import CompanyMergePersistenceError, StalePendingReviewError
from ps_service.logging import emit_log_entry
from ps_service.logging.errors import LoggingLifecycleError

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from ps_service.api.near_miss_audit_actions import NearMissResolveReason
    from ps_service.audit import AuditContext
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
    from ps_service.config import ServiceConfig
    from ps_service.logging import LogEmitter

_COMPONENT = "near_miss_review"


@dataclass(frozen=True, slots=True)
class NearMissReviewDependencies:
    """Everything the near-miss review routes need that is not per-request."""

    open_single_tenant_graph: Callable[[ServiceConfig], GraphHandle]
    list_pending_reviews: Callable[[GraphHandle], tuple[PendingReviewRecord, ...]]
    resolve_review: Callable[
        [GraphHandle, str, Literal["keep-separate", "merge"]], ResolveOutcome | None
    ]
    get_pending_review: Callable[[GraphHandle, str], PendingReviewRecord | None]


def _to_pending_review_entry(record: PendingReviewRecord) -> PendingReviewEntry:
    """Map one `PendingReviewRecord` to the `GET /near-misses` wire entry (AC-BI-003).

    `id`/`kind`/`incoming_text`/`nearest_existing_text`/`similarity` only --
    `incoming_id`/`nearest_existing_id` are deliberately omitted from the
    wire response (PLAN.md §3.2: AC-BI-003 only requires the review's OWN
    id, incoming text, existing text, and similarity score).
    """
    return PendingReviewEntry(
        id=record.id,
        kind=record.kind,
        incoming_text=record.incoming_text,
        nearest_existing_text=record.nearest_existing_text,
        similarity=record.similarity,
    )


def run_list_near_misses(
    *, config: ServiceConfig, dependencies: NearMissReviewDependencies
) -> PendingReviewListResponse:
    """Return every unresolved `PendingReview` as a `PendingReviewListResponse` (AC-BI-003).

    Args:
        config: The resolved service configuration (injected).
        dependencies: The near-miss review dependency bundle (injected;
            overridden in tests).

    Returns:
        A :class:`PendingReviewListResponse` carrying one entry per
        unresolved `PendingReview` node, in the order
        `pending_review.list_pending_reviews` returned them.
    """
    graph = dependencies.open_single_tenant_graph(config)
    records = dependencies.list_pending_reviews(graph)
    return PendingReviewListResponse(reviews=[_to_pending_review_entry(r) for r in records])


def _to_resolve_response(outcome: ResolveOutcome) -> ResolveReviewResponse:
    """Map a `ResolveOutcome` to the `POST /near-misses/{review_id}/resolve` success body."""
    return ResolveReviewResponse(
        review_id=outcome.review_id,
        decision=outcome.decision,
        winner_id=outcome.winner_id,
        loser_id=outcome.loser_id,
    )


def _failure_reason(exc: Exception) -> NearMissResolveReason:
    """Map a failed resolution to the enumerated audit `reason_code` (never the message)."""
    if isinstance(exc, PendingReviewNotFoundError):
        # A stale merge is translated to this API error with the stale signal as its cause.
        return (
            "review_stale"
            if isinstance(exc.__cause__, StalePendingReviewError)
            else "review_not_found"
        )
    if isinstance(exc, CompanyMergePersistenceError):
        return "graph_write_failed"
    if isinstance(exc, redis.exceptions.RedisError):
        return "graph_unavailable"
    return "unexpected_error"


def _emit_resolve_log(
    *,
    record: PendingReviewRecord,
    decision: Literal["keep-separate", "merge"],
    approval_id: str | None,
    outcome: Literal["succeeded", "failed"],
    reason: NearMissResolveReason | None,
    emitter: LogEmitter | None,
) -> None:
    """One semantic entry per resolution so the log and the audit trail agree (ids only)."""
    extra: dict[str, object] = {"decision": decision}
    if approval_id is not None:
        extra["approval_id"] = approval_id
    if reason is not None:
        extra["reason_code"] = reason
    try:
        emit_log_entry(
            component=_COMPONENT,
            action="resolve_near_miss",
            entity_id=record.id,
            outcome=outcome,
            extra=extra,
            emitter=emitter,
        )
    except LoggingLifecycleError:
        return


def run_resolve_near_miss(
    review_id: str,
    decision: Literal["keep-separate", "merge"],
    *,
    config: ServiceConfig,
    dependencies: NearMissReviewDependencies,
    audit: AuditContext,
    approval_id: str | None = None,
    emitter: LogEmitter | None = None,
) -> ResolveReviewResponse:
    """Resolve one `PendingReview` (issue #35, `POST /near-misses/{review_id}/resolve`).

    AC-BI-004: `decision="keep-separate"` deletes only the `PendingReview`
    record, nothing else. AC-BI-005/006/007: `decision="merge"` re-points
    every edge referencing the loser canonical node onto the
    deterministically-chosen winner, deletes the loser, and deletes the
    `PendingReview` record, all as one atomic write.

    Audit (issue #195, AC-BI-012/011): after the review is found and before any write, a
    `near_miss.resolve` `applied` row is recorded (fail-closed: if it cannot be written nothing is
    resolved and `AuditTrailUnavailableError` propagates). If the resolution then fails, a
    best-effort `failed` row with an enumerated `reason_code` follows and the original exception is
    re-raised unchanged. An unknown review writes no row (nothing to audit, D-G). `audit.actor` is
    the principal who performed the operation (for a passkey-approved merge, the approver, with
    `approval_id` set).

    AC-BI-008 (not-found half, both decisions): raises
    `PendingReviewNotFoundError` (-> HTTP 404) when `review_id` doesn't
    exist or was already resolved -- the API-boundary translation of
    `pending_review.resolve_review`'s `None` return (see module docstring);
    no write happens on this path. For `decision="merge"` specifically,
    also raises `PendingReviewNotFoundError` (same status, a dedicated
    "stale" message) when `pending_review.resolve_review` raises
    `StalePendingReviewError` -- the review exists but the incoming/existing
    canonical node it references no longer does (already resolved by a
    prior merge); no merge write happens on this path either (CHANGES.md
    H2).

    Args:
        review_id: The `PendingReview` id to resolve (path parameter).
        decision: How to resolve it -- `"keep-separate"` or `"merge"`.
        config: The resolved service configuration (injected).
        dependencies: The near-miss review dependency bundle (injected;
            overridden in tests).
        audit: Who is acting and where their audit rows go.
        approval_id: The signed approval authorising a merge, recorded in the audit details.
        emitter: Optional log emitter override (tests).

    Returns:
        A :class:`ResolveReviewResponse` naming the resolved review and
        decision (`winner_id`/`loser_id` populated for `"merge"` only).

    Raises:
        PendingReviewNotFoundError: `review_id` doesn't exist, was already
            resolved, or (merge only) references a node a prior merge
            already deleted.
        AuditTrailUnavailableError: the opening audit row could not be written; nothing was
            resolved.
    """
    graph = dependencies.open_single_tenant_graph(config)
    record = dependencies.get_pending_review(graph, review_id)
    if record is None:
        raise PendingReviewNotFoundError(f"no unresolved PendingReview with id {review_id!r}")
    target = AuditTarget(NEAR_MISS_RESOLVE_ACTION, NEAR_MISS_REVIEW_RESOURCE_TYPE, record.id)
    details: dict[str, object] = {
        "review_id": record.id,
        "kind": record.kind,
        "incoming_id": record.incoming_id,
        "existing_id": record.nearest_existing_id,
        "decision": "keep_separate" if decision == "keep-separate" else "merge",
    }
    if approval_id is not None:
        details["approval_id"] = approval_id
    record_opening_row(audit, target, component=_COMPONENT, details=details, emitter=emitter)
    try:
        response = _resolve_or_raise_not_found(dependencies, graph, review_id, decision)
    except Exception as exc:
        reason = _failure_reason(exc)
        record_follow_up_row(
            audit,
            target,
            component=_COMPONENT,
            outcome="failed",
            details={**details, "reason_code": reason},
            emitter=emitter,
        )
        _emit_resolve_log(
            record=record,
            decision=decision,
            approval_id=approval_id,
            outcome="failed",
            reason=reason,
            emitter=emitter,
        )
        raise
    _emit_resolve_log(
        record=record,
        decision=decision,
        approval_id=approval_id,
        outcome="succeeded",
        reason=None,
        emitter=emitter,
    )
    return response


def _resolve_or_raise_not_found(
    dependencies: NearMissReviewDependencies,
    graph: GraphHandle,
    review_id: str,
    decision: Literal["keep-separate", "merge"],
) -> ResolveReviewResponse:
    """Run `resolve_review`, translating its not-found/stale signals into API errors."""
    try:
        outcome = dependencies.resolve_review(graph, review_id, decision)
    except StalePendingReviewError as exc:
        raise PendingReviewNotFoundError(
            f"pending review {review_id!r} references a node that no longer exists "
            "(already resolved by a prior merge); this review is now stale"
        ) from exc
    if outcome is None:
        raise PendingReviewNotFoundError(f"no unresolved PendingReview with id {review_id!r}")
    return _to_resolve_response(outcome)


# --- default wiring (M6 -- every company_merge import below is function-local) ---


def _open_single_tenant_graph(config: ServiceConfig) -> GraphHandle:
    """Open the single-tenant (``policy_system``) graph.

    Byte-for-byte copy of `ingestion_orchestration._open_single_tenant_graph`
    (CHANGES.md M1) -- an independent copy, not a shared import, mirroring
    this component's established "own copy per module" convention.
    """
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
        single_tenant_graph_name,
    )

    return select_graph(connect_from_config(config), single_tenant_graph_name())


def build_default_near_miss_review_graph_opener() -> Callable[[ServiceConfig], GraphHandle]:
    """Return the real single-tenant graph opener.

    The *only* moving part `build_default_near_miss_review_dependencies` lets a
    caller substitute (issue #163 Slice D) -- the true infra boundary (one
    FalkorDB client construction, `_open_single_tenant_graph`), never the real
    `list_pending_reviews`/`resolve_review` business logic sitting on top of
    it. Extracted to its own top-level function (rather than inlined in
    `build_default_near_miss_review_dependencies`) specifically so it is its
    own, independently addressable module-level name: a caller-side
    `monkeypatch.setattr("ps_service.api.near_miss_review_orchestration.
    build_default_near_miss_review_graph_opener", ...)` substitutes the
    opener alone, while `build_default_near_miss_review_dependencies` itself
    -- called with no arguments -- still resolves this name at call time
    (ordinary Python late-binding for a bare module-level call, mirroring
    `ingestion_orchestration.build_default_graph_openers`'s own
    issue #163 Slice C precedent) and so picks up the substitution
    automatically, with zero change to its own call sites.
    `docs/coding-standards/approved-mock-boundaries.yaml` lists this function
    itself as the approved boundary -- not
    `build_default_near_miss_review_dependencies`, which stays off that list
    since it still bundles real business logic alongside this boundary.

    Returns:
        `_open_single_tenant_graph`, bound to the production single-tenant
        FalkorDB graph opener.
    """
    return _open_single_tenant_graph


def build_default_near_miss_review_dependencies(
    *, open_single_tenant_graph: Callable[[ServiceConfig], GraphHandle] | None = None
) -> NearMissReviewDependencies:
    """Wire the real `pending_review` functions into a `NearMissReviewDependencies`.

    `ps_service.company_merge.pending_review` is imported **function-locally**
    so that importing `ps_service.main` never transitively loads
    `ps_service.company_merge` at module load (M6) -- mirrors
    `build_default_restore_dependencies`/`build_default_pipeline_dependencies`
    exactly.

    issue #163 Slice D narrowed this factory's own DI seam:
    `open_single_tenant_graph` is the *only* substitutable parameter. There
    is deliberately no `list_pending_reviews`/`resolve_review` parameter any
    more -- both are always the real, shipped `pending_review` functions,
    unconditionally, with no way for a caller (test or otherwise) to
    substitute fake business logic through this function's own signature.
    `open_single_tenant_graph=None` (the default -- every production caller,
    unchanged) resolves `build_default_near_miss_review_graph_opener()` at
    call time, so a `monkeypatch.setattr` of *that* function (see its own
    docstring) is picked up automatically even though this factory itself
    is never patched.

    Args:
        open_single_tenant_graph: The single-tenant graph opener to use, or
            `None` (every production caller) to use the real one.

    Returns:
        A :class:`NearMissReviewDependencies` bound to the production
        `list_pending_reviews`/`resolve_review` and the given (or real)
        single-tenant-graph opener.
    """
    from ps_service.company_merge.pending_review import (  # noqa: PLC0415 -- M6: function-local
        get_pending_review,
        list_pending_reviews,
        resolve_review,
    )

    return NearMissReviewDependencies(
        open_single_tenant_graph=(
            open_single_tenant_graph or build_default_near_miss_review_graph_opener()
        ),
        list_pending_reviews=list_pending_reviews,
        resolve_review=resolve_review,
        get_pending_review=get_pending_review,
    )
