"""REST-boundary glue for the near-miss review workflow.

A near miss is a pair of similar entities that Company Merge left separate and queued as a
`PendingReview` for a human decision. This module serves the two operations behind the REST
routes and the MCP tools: listing the unresolved reviews, and resolving one as `keep-separate`.
Merging near misses is a separate flow, `graph_cleanup.near_miss_batch`, which previews a chosen
set and binds it to one passkey approval.

It provides the injection seam :class:`NearMissReviewDependencies`, the production wiring
:func:`build_default_near_miss_review_dependencies`, and the entry points
:func:`run_list_near_misses` and :func:`run_resolve_near_miss`.

The production wiring imports `ps_service.company_merge` function-locally, so importing
`ps_service.main` never loads Company Merge at module load. Only the graph opener is
substitutable through the factory (see :func:`build_default_near_miss_review_graph_opener`):
the review listing and resolution logic is always the real `pending_review` code, so tests
replace the FalkorDB boundary and never the business rules above it.

Lower-layer outcomes are translated into API errors here, because `ps_service.company_merge`
never imports `ps_service.api`: `pending_review.resolve_review` returning `None` (unknown or
already resolved review) becomes :class:`PendingReviewNotFoundError`, before any write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import redis.exceptions

from ps_service.api.errors import PendingReviewNotFoundError, ReviewKindUnsupportedError
from ps_service.api.graph_write_access import graph_write_access
from ps_service.api.models import (
    PendingReviewEntry,
    PendingReviewListResponse,
    ResolveReviewResponse,
)
from ps_service.api.near_miss_audit_actions import (
    NEAR_MISS_RESOLVE_ACTION,
    NEAR_MISS_REVIEW_RESOURCE_TYPE,
)
from ps_service.audit import (
    AuditTarget,
    record_follow_up_row,
    record_opening_row_with_event_id,
)
from ps_service.company_merge.errors import (
    CompanyMergePersistenceError,
    UnknownReviewKindError,
)
from ps_service.logging import emit_log_entry
from ps_service.logging.errors import LoggingLifecycleError

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from ps_service.api.near_miss_audit_actions import NearMissResolveReason
    from ps_service.audit import AuditContext
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.graph_submission import GraphWriteTarget
    from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
    from ps_service.config import ServiceConfig
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

_COMPONENT = "near_miss_review"


@dataclass(frozen=True, slots=True)
class NearMissReviewDependencies:
    """Everything the near-miss review entry points need that is not per-request.

    Attributes:
        open_single_tenant_graph: Opens the configured single-tenant graph for a resolved
            service configuration. May raise a Redis error when FalkorDB is unreachable.
        list_pending_reviews: Returns every unresolved `PendingReviewRecord` in the graph.
        resolve_review: Keeps one review's pair separate by id, writing through the given write
            target. Returns the `ResolveOutcome`, or `None` when the id is unknown or already
            resolved.
        get_pending_review: Returns the `PendingReviewRecord` with the given id, or `None` when no
            unresolved review has it.
    """

    open_single_tenant_graph: Callable[[ServiceConfig], GraphHandle]
    list_pending_reviews: Callable[[GraphHandle], tuple[PendingReviewRecord, ...]]
    resolve_review: Callable[
        [GraphHandle, str, Literal["keep-separate"], GraphWriteTarget],
        ResolveOutcome | None,
    ]
    get_pending_review: Callable[[GraphHandle, str], PendingReviewRecord | None]


def _to_pending_review_entry(record: PendingReviewRecord) -> PendingReviewEntry:
    """Map one `PendingReviewRecord` to the wire entry of the near-miss listing.

    Only the review id, kind, both texts and the similarity are exposed; the internal
    `incoming_id` and `nearest_existing_id` node ids are deliberately omitted.
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
    """Return every unresolved `PendingReview` as a `PendingReviewListResponse`.

    Args:
        config: The resolved service configuration, used to open the graph.
        dependencies: The near-miss review dependency bundle that supplies the graph opener and
            the review listing.

    Returns:
        A :class:`PendingReviewListResponse` with one entry per unresolved review, in the order
        `list_pending_reviews` returned them; an empty `reviews` list when none are pending.

    Raises:
        redis.exceptions.RedisError: The graph is unreachable. Nothing is written.
        ReviewKindUnsupportedError: A stored review has a kind with no registered spec (HTTP 409,
            naming the kind). The listing fails closed rather than omitting the review.
    """
    graph = dependencies.open_single_tenant_graph(config)
    try:
        records = dependencies.list_pending_reviews(graph)
    except UnknownReviewKindError as exc:
        raise ReviewKindUnsupportedError(str(exc)) from exc
    return PendingReviewListResponse(reviews=[_to_pending_review_entry(r) for r in records])


def _to_resolve_response(outcome: ResolveOutcome) -> ResolveReviewResponse:
    """Map a `ResolveOutcome` to the success body of the resolve route."""
    return ResolveReviewResponse(review_id=outcome.review_id, decision=outcome.decision)


def _failure_reason(exc: Exception) -> NearMissResolveReason:
    """Map a failed resolution to the enumerated audit `reason_code`, never the message."""
    if isinstance(exc, PendingReviewNotFoundError):
        return "review_not_found"
    if isinstance(exc, CompanyMergePersistenceError):
        return "graph_write_failed"
    if isinstance(exc, redis.exceptions.RedisError):
        return "graph_unavailable"
    return "unexpected_error"


def _emit_resolve_log(
    *,
    record: PendingReviewRecord,
    outcome: Literal["succeeded", "failed"],
    reason: NearMissResolveReason | None,
    emitter: LogEmitter | None,
) -> None:
    """Emit one semantic log entry per resolution so the log and the audit trail agree (ids only).

    A logging lifecycle failure is swallowed: it never fails the resolution.
    """
    extra: dict[str, object] = {"decision": "keep-separate"}
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
        return  # diagnostics only


def run_resolve_near_miss(
    review_id: str,
    decision: Literal["keep-separate"],
    *,
    config: ServiceConfig,
    dependencies: NearMissReviewDependencies,
    audit: AuditContext,
    emitter: LogEmitter | None = None,
) -> ResolveReviewResponse:
    """Resolve one `PendingReview` by keeping the pair separate.

    Deletes only the `PendingReview` record. A merge is not decided here: it is requested through
    `merge-near-misses` and carried out when its approval is signed.

    Args:
        review_id: The `PendingReview` id to resolve (the route's path parameter).
        decision: `"keep-separate"`.
        config: The resolved service configuration, used to open the graph and find the gateway.
        dependencies: The near-miss review dependency bundle that supplies the graph opener, the
            review lookup and the resolution.
        audit: The acting principal and the destination of the audit rows.
        emitter: Log emitter override; `None` uses the process default.

    Returns:
        A :class:`ResolveReviewResponse` naming the resolved review and the decision;
        `winner_id` and `loser_id` are always `None`.

    Raises:
        PendingReviewNotFoundError: `review_id` does not exist or was already resolved. No write
            happens, and an unknown review writes no audit row.
        ReviewKindUnsupportedError: The review has a kind with no registered spec (HTTP 409,
            naming the kind). Raised before the audit row, so nothing is audited or resolved.
        GraphWriteUnavailableError: No Graph Write Gateway is installed (HTTP 503). Checked after
            the not-found check and before the opening audit row, so nothing was audited or
            resolved.
        AuditTrailUnavailableError: The opening audit row could not be written; nothing was
            resolved.
        CompanyMergePersistenceError: The graph write failed; the failure is audited and the
            exception is re-raised unchanged.
        redis.exceptions.RedisError: The graph is unreachable (when opening it or while
            resolving); when it occurs during resolution the failure is audited and re-raised.

    Side Effects:
        After the review is found and before any write, records a `near_miss.resolve` audit row
        with outcome `applied` (fail-closed). Writes the resolution to the graph through the
        gateway, grouped under that audit row's event id. If the resolution then fails, records a
        best-effort `failed` audit row carrying an enumerated `reason_code` and re-raises the
        original exception. Emits one log entry (`resolve_near_miss`) with the outcome.
    """
    graph = dependencies.open_single_tenant_graph(config)
    try:
        record = dependencies.get_pending_review(graph, review_id)
    except UnknownReviewKindError as exc:
        raise ReviewKindUnsupportedError(str(exc)) from exc
    if record is None:
        raise PendingReviewNotFoundError(f"no unresolved PendingReview with id {review_id!r}")
    # The resolution is written through the Graph Write Gateway, so with none installed
    # it fails closed here -- after the not-found check (an unknown review is still a 404) and
    # before the opening audit row (nothing is audited for a resolution that cannot run).
    write_access = graph_write_access(config)
    target = AuditTarget(NEAR_MISS_RESOLVE_ACTION, NEAR_MISS_REVIEW_RESOURCE_TYPE, record.id)
    details: dict[str, object] = {
        "review_id": record.id,
        "kind": record.kind,
        "incoming_id": record.incoming_id,
        "existing_id": record.nearest_existing_id,
        "decision": "keep_separate",
    }
    # The opening row's id anchors the resolution's graph group to the audit trail.
    audit_event_id = record_opening_row_with_event_id(
        audit, target, component=_COMPONENT, details=details, emitter=emitter
    )
    write_target = _graph_write_target(write_access.gateway, config.falkordb_graph, audit_event_id)
    try:
        response = _resolve_or_raise_not_found(
            dependencies, graph, review_id, decision, write_target
        )
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
            outcome="failed",
            reason=reason,
            emitter=emitter,
        )
        raise
    _emit_resolve_log(
        record=record,
        outcome="succeeded",
        reason=None,
        emitter=emitter,
    )
    return response


def _graph_write_target(
    gateway: GroupSubmitter, graph_name: str, audit_event_id: str
) -> GraphWriteTarget:
    """Build the write target: the graph `graph_name`, under the opening audit event."""
    from ps_service.company_merge.graph_submission import (  # noqa: PLC0415 -- function-local, as above
        GraphWriteTarget,
    )

    return GraphWriteTarget(gateway, graph_name, audit_event_id)


def _resolve_or_raise_not_found(
    dependencies: NearMissReviewDependencies,
    graph: GraphHandle,
    review_id: str,
    decision: Literal["keep-separate"],
    write_target: GraphWriteTarget,
) -> ResolveReviewResponse:
    """Run `resolve_review`, translating its not-found signal into an API error.

    Raises:
        PendingReviewNotFoundError: `resolve_review` returned `None` (unknown or already resolved
            review).
        CompanyMergePersistenceError: Propagated when the graph write fails.
    """
    outcome = dependencies.resolve_review(graph, review_id, decision, write_target)
    if outcome is None:
        raise PendingReviewNotFoundError(f"no unresolved PendingReview with id {review_id!r}")
    return _to_resolve_response(outcome)


# --- default wiring (every company_merge import below is function-local) ---


def _open_single_tenant_graph(config: ServiceConfig) -> GraphHandle:
    """Open the graph `config.falkordb_graph` names, connecting from the service configuration.

    The import is function-local so `ps_service.main` stays independent of Company Merge at
    import time.

    Raises:
        redis.exceptions.RedisError: FalkorDB cannot be reached.
    """
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
    )

    return select_graph(connect_from_config(config), config.falkordb_graph)


def build_default_near_miss_review_graph_opener() -> Callable[[ServiceConfig], GraphHandle]:
    """Return the production single-tenant graph opener.

    This is the only part of the default dependencies a caller may substitute: the true
    infrastructure boundary (one FalkorDB client construction), never the review listing and
    resolution logic above it. It is a top-level function so it is addressable on its own: patching
    this name replaces the opener alone, and
    :func:`build_default_near_miss_review_dependencies` resolves it at call time and picks the
    substitute up.

    Returns:
        The function that opens the graph a service configuration names.
    """
    return _open_single_tenant_graph


def build_default_near_miss_review_dependencies(
    *, open_single_tenant_graph: Callable[[ServiceConfig], GraphHandle] | None = None
) -> NearMissReviewDependencies:
    """Wire the real `pending_review` functions into a `NearMissReviewDependencies`.

    `ps_service.company_merge.pending_review` is imported function-locally so importing
    `ps_service.main` never loads Company Merge at module load. The review listing, lookup and
    resolution are always the real functions; only the graph opener can be substituted.

    Args:
        open_single_tenant_graph: The graph opener to use. `None` (the production default)
            uses the one returned by :func:`build_default_near_miss_review_graph_opener`.

    Returns:
        A :class:`NearMissReviewDependencies` bound to the production `list_pending_reviews`,
        `resolve_review` and `get_pending_review`, and to the given (or default) graph opener.
    """
    from ps_service.company_merge.pending_review import (  # noqa: PLC0415 -- function-local
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
