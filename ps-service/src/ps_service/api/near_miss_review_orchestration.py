"""REST-boundary glue for the near-miss review workflow.

A near miss is a pair of similar entities that Company Merge left separate and queued as a
`PendingReview` for a human decision. This module serves the two operations behind the REST
routes: listing the unresolved reviews, and resolving one as `keep-separate` or `merge`.

It provides the injection seam :class:`NearMissReviewDependencies`, the production wiring
:func:`build_default_near_miss_review_dependencies`, and the entry points
:func:`run_list_near_misses`, :func:`run_resolve_near_miss`, :func:`run_request_near_miss_merge`
and :func:`check_near_miss_merge_approval`.

A near-miss merge is not a separate write. :func:`run_request_near_miss_merge` picks the survivor,
builds the graph-cleanup preview for the pair and creates that approval with the review id signed
into it; the registered cleanup executor then runs the merge when the approval is signed and
deletes the `PendingReview` in the same write group. A Capability review uses `merge-capabilities`
(`REQUIRES`/`COVERS`/`MITIGATED_BY` edges moved, the absorbed Capability deleted and a
`MergedCapability` marker left, the one-Policy rule enforced, a `capability.merge` audit row with
snapshots). A Role review uses `merge-roles` (the absorbed Role's Obligations move to the survivor,
the absorbed Role is replaced by a `MergedRole` marker, a `role.merge` audit row with snapshots);
a Role has no governing Policy, so the governance acknowledgment does not apply to it. This module
is the glue between the two components, because `ps_service.company_merge` must not import
`ps_service.graph_cleanup`.

The production wiring imports `ps_service.company_merge` function-locally, so importing
`ps_service.main` never loads Company Merge at module load. Only the graph opener is
substitutable through the factory (see :func:`build_default_near_miss_review_graph_opener`):
the review listing and resolution logic is always the real `pending_review` code, so tests
replace the FalkorDB boundary and never the business rules above it.

Lower-layer outcomes are translated into API errors here, because `ps_service.company_merge`
never imports `ps_service.api`: `pending_review.resolve_review` returning `None` (unknown or
already resolved review) and `StalePendingReviewError` (a merge request whose incoming or existing
node no longer exists) both become :class:`PendingReviewNotFoundError`, before any write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

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
    StalePendingReviewError,
    UnknownReviewKindError,
)
from ps_service.company_merge.pending_review import choose_survivor, read_review_merge_sides
from ps_service.company_merge.reviewable_kinds import CAPABILITY, ROLE
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.executors import TOOL_MERGE_CAPABILITIES, TOOL_MERGE_ROLES
from ps_service.graph_cleanup.models import CapabilityMergePreview
from ps_service.graph_cleanup.service import (
    check_cleanup_approval,
    create_capability_merge_approval,
    create_role_merge_approval,
)
from ps_service.logging import emit_log_entry
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.passkey_signing.service import PendingApprovalStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from ps_service.api.near_miss_audit_actions import NearMissResolveReason
    from ps_service.audit import AuditContext
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.graph_submission import GraphWriteTarget
    from ps_service.company_merge.models import PendingReviewRecord, ResolveOutcome
    from ps_service.company_merge.pending_review import ReviewMergeSides
    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.graph_cleanup.service import CapabilityMergeApproval, RoleMergeApproval
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter
    from ps_service.passkey_signing.store import PendingApprovalStore

_COMPONENT = "near_miss_review"
_STALE_REVIEW_MESSAGE: Final = (
    "pending review {review_id!r} references a node that no longer exists "
    "(already resolved by a prior merge); this review is now stale"
)


@dataclass(frozen=True, slots=True)
class NearMissReviewDependencies:
    """Everything the near-miss review entry points need that is not per-request.

    Attributes:
        open_single_tenant_graph: Opens the single-tenant `policy_system` graph for a resolved
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
    return ResolveReviewResponse(
        review_id=outcome.review_id,
        decision=outcome.decision,
        winner_id=outcome.winner_id,
        loser_id=outcome.loser_id,
    )


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

    Deletes only the `PendingReview` record. A Capability merge is not decided here:
    :func:`run_request_near_miss_merge` creates the graph-cleanup `merge-capabilities` approval and
    the cleanup executor writes the merge (edges moved, the absorbed Capability deleted behind a
    `MergedCapability` marker, one Policy rule) when it is signed.

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
    write_target = _graph_write_target(write_access.gateway, audit_event_id)
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


def _graph_write_target(gateway: GroupSubmitter, audit_event_id: str) -> GraphWriteTarget:
    """Build the write target: the `policy_system` graph, under the opening audit event."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off Company Merge at import
        single_tenant_graph_name,
    )
    from ps_service.company_merge.graph_submission import (  # noqa: PLC0415 -- function-local, as above
        GraphWriteTarget,
    )

    return GraphWriteTarget(gateway, single_tenant_graph_name(), audit_event_id)


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
    """Open the single-tenant (`policy_system`) graph from the service configuration.

    The import is function-local so `ps_service.main` stays independent of Company Merge at
    import time.

    Raises:
        redis.exceptions.RedisError: FalkorDB cannot be reached.
    """
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
        single_tenant_graph_name,
    )

    return select_graph(connect_from_config(config), single_tenant_graph_name())


def build_default_near_miss_review_graph_opener() -> Callable[[ServiceConfig], GraphHandle]:
    """Return the production single-tenant graph opener.

    This is the only part of the default dependencies a caller may substitute: the true
    infrastructure boundary (one FalkorDB client construction), never the review listing and
    resolution logic above it. It is a top-level function so it is addressable on its own: patching
    this name replaces the opener alone, and
    :func:`build_default_near_miss_review_dependencies` resolves it at call time and picks the
    substitute up.

    Returns:
        The function that opens the `policy_system` graph for a service configuration.
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


_ROLE_ACKNOWLEDGMENT_MESSAGE: Final = (
    "a Role merge has no governing Policy; acknowledge_governance_change does not apply"
)


@dataclass(frozen=True, slots=True)
class _MergeRequest:
    """What both kinds of near-miss merge request need to create their approval.

    Attributes:
        graph: Read handle on the single-tenant graph.
        sides: The review's two sides, as read under its own kind.
        acknowledge_governance_change: The caller's acknowledgment flag.
        actor: Verified `(subject, issuer)` of the requesting Compliance Officer.
        base_url: Origin used to build the approval link.
        store: Pending-approval store that receives the approval row.
    """

    graph: GraphHandle
    sides: ReviewMergeSides
    acknowledge_governance_change: bool
    actor: tuple[str, str]
    base_url: str
    store: PendingApprovalStore


def _request_role_merge(request: _MergeRequest) -> RoleMergeApproval:
    """Create the `merge-roles` approval for a Role review.

    A Role has no governing Policy, so an acknowledgment of a governance change is refused rather
    than ignored. The survivor is the incoming Role: a Role node carries no `created_at`, so
    `choose_survivor` always decides for the node whose arrival raised the near miss.

    Args:
        request: The review's sides and the caller's request context.

    Returns:
        The Role merge preview with the approval id, signing URL and expiry.

    Raises:
        GraphCleanupValidationError: `acknowledge_governance_change` is true, or the Role merge
            refuses the pair or the review.
        GraphCleanupPersistenceError: The graph could not be read.
    """
    if request.acknowledge_governance_change:
        raise GraphCleanupValidationError(_ROLE_ACKNOWLEDGMENT_MESSAGE)
    survivor_id, absorbed_id = choose_survivor(request.sides)
    return create_role_merge_approval(
        request.graph,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        review_id=request.sides.review_id,
        actor=request.actor,
        base_url=request.base_url,
        store=request.store,
    )


def _request_capability_merge(request: _MergeRequest) -> CapabilityMergeApproval:
    """Create the `merge-capabilities` approval for a Capability review.

    The survivor is the Capability with the earlier `created_at` (a missing one counts earliest, a
    tie goes to the incoming node).

    Args:
        request: The review's sides and the caller's request context.

    Returns:
        The Capability merge preview with the approval id, signing URL and expiry.

    Raises:
        GraphCleanupAcknowledgmentRequiredError: Case 2 without the acknowledgment.
        GraphCleanupValidationError: The cleanup merge refuses the pair.
        GraphCleanupPersistenceError: The graph could not be read.
    """
    survivor_id, absorbed_id = choose_survivor(request.sides)
    return create_capability_merge_approval(
        request.graph,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        acknowledge_governance_change=request.acknowledge_governance_change,
        review_id=request.sides.review_id,
        actor=request.actor,
        base_url=request.base_url,
        store=request.store,
    )


def run_request_near_miss_merge(
    review_id: str,
    *,
    actor: tuple[str, str],
    acknowledge_governance_change: bool,
    base_url: str,
    config: ServiceConfig,
    dependencies: NearMissReviewDependencies,
    store: PendingApprovalStore,
) -> CapabilityMergeApproval | RoleMergeApproval:
    """Preview merging a near-miss pair and create the graph-cleanup merge approval for its kind.

    A Capability review gets a `merge-capabilities` approval; the survivor is the Capability with
    the earlier `created_at` (a missing one counts earliest, a tie goes to the incoming node). A
    Role review gets a `merge-roles` approval; a Role carries no `created_at`, so the survivor is
    the incoming Role. The approval is bound to the pair and the state digest, with the review id
    signed into it; signing runs the cleanup executor, which merges and deletes the `PendingReview`
    in one write group. Nothing is written to the graph here.

    Args:
        review_id: Id of the near-miss review, as listed by `near_misses_list`.
        actor: Verified `(subject, issuer)` of the requesting Compliance Officer; the approval is
            bound to them. The caller has already checked the `ComplianceOfficer` grant.
        acknowledge_governance_change: True when the officer accepts the governance change of a
            case-2 Capability merge (exactly one Capability governed). Refused for a Role review,
            which has no governing Policy.
        base_url: Origin used to build the approval link, without a trailing slash.
        config: The resolved service configuration, used to open the graph.
        dependencies: The near-miss review dependency bundle that supplies the graph opener.
        store: Pending-approval store that receives the approval row.

    Returns:
        The cleanup merge preview with the approval id, signing URL and expiry: a
        `CapabilityMergeApproval` for a Capability review, a `RoleMergeApproval` for a Role review.

    Raises:
        PendingReviewNotFoundError: `review_id` does not exist or was already resolved, or one of
            its nodes no longer exists (stale review, own message).
        ReviewKindUnsupportedError: The review has a kind with no registered spec (HTTP 409,
            naming the kind).
        GraphCleanupValidationError: The review is of a registered kind that has no merge (the
            message names the kind), `acknowledge_governance_change` was set for a Role review, or
            the cleanup merge refuses the pair (Capability: different governing Policies, a side
            that is not active).
        GraphCleanupAcknowledgmentRequiredError: Case 2 Capability merge without the
            acknowledgment; carries the preview and no approval exists.
        GraphCleanupPersistenceError: The graph could not be read.

    Side Effects:
        Reads the graph and inserts one pending-approval row on success; emits one log entry.
    """
    graph = dependencies.open_single_tenant_graph(config)
    try:
        sides = read_review_merge_sides(graph, review_id)
    except StalePendingReviewError as exc:
        raise PendingReviewNotFoundError(_STALE_REVIEW_MESSAGE.format(review_id=review_id)) from exc
    except UnknownReviewKindError as exc:
        raise ReviewKindUnsupportedError(str(exc)) from exc
    except redis.exceptions.RedisError as exc:
        raise GraphCleanupPersistenceError("the policy graph could not be read") from exc
    if sides is None:
        raise PendingReviewNotFoundError(f"no unresolved PendingReview with id {review_id!r}")
    merge_request = _MergeRequest(
        graph=graph,
        sides=sides,
        acknowledge_governance_change=acknowledge_governance_change,
        actor=actor,
        base_url=base_url,
        store=store,
    )
    if sides.kind == ROLE.label:
        approval: CapabilityMergeApproval | RoleMergeApproval = _request_role_merge(merge_request)
    elif sides.kind == CAPABILITY.label:
        approval = _request_capability_merge(merge_request)
    else:
        raise GraphCleanupValidationError(
            f"a {sides.kind} near miss cannot be merged; keep it separate"
        )
    extra: dict[str, object] = {
        "kind": sides.kind,
        "approval_id": approval.pending_approval_id,
        "survivor_id": approval.preview.survivor_id,
        "absorbed_id": approval.preview.absorbed_id,
    }
    if isinstance(approval.preview, CapabilityMergePreview):
        extra["policy_case"] = approval.preview.policy_case
    emit_log_entry(
        component=_COMPONENT,
        action="request_near_miss_merge",
        entity_id=review_id,
        outcome="succeeded",
        extra=extra,
    )
    return approval


def check_near_miss_merge_approval(
    *,
    pending_approval_id: str,
    actor: tuple[str, str] | None,
    store: PendingApprovalStore,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
) -> PendingApprovalStatus | None:
    """Report a near-miss merge approval, mapping the cleanup approval's fields to winner/loser.

    A near-miss merge approval is a `merge-capabilities` or `merge-roles` cleanup approval that
    carries a review id. Any other approval, an unknown id, another caller's approval and a
    missing actor all return None, indistinguishably.

    Args:
        pending_approval_id: Id returned when the merge was requested.
        actor: The caller's verified `(subject, issuer)`, or None when there is none.
        store: Pending-approval store.
        config: Service configuration, used when an orphaned signed approval is reconciled.
        dependencies: Graph-cleanup dependencies for that reconciliation.

    Returns:
        The status: `winner_id` (the survivor) and `loser_id` (the absorbed node) once the
        signed merge has run, the executor's safe `error` text when it did not, otherwise None for
        all three; None when no such approval is visible to the caller.

    Raises:
        GraphCleanupPersistenceError: Reconciliation could not read the graph.
        AuditPostgresUnavailableError: Reconciliation could not record its audit row.
        AuditPersistenceError: As above, for any other audit write failure.

    Side Effects:
        As `check_cleanup_approval`: a signed approval whose outcome was never recorded may be
        reconciled against the graph.
    """
    status = check_cleanup_approval(
        pending_approval_id=pending_approval_id,
        actor=actor,
        store=store,
        config=config,
        dependencies=dependencies,
    )
    if (
        status is None
        or status.tool_name not in (TOOL_MERGE_CAPABILITIES, TOOL_MERGE_ROLES)
        or status.review_id is None
    ):
        return None
    outcome = status.outcome or {}
    winner = outcome.get("survivor_id")
    loser = outcome.get("absorbed_id")
    error = outcome.get("error")
    return PendingApprovalStatus(
        pending_approval_id=status.pending_approval_id,
        status=status.status,
        review_id=status.review_id,
        decision="merge",
        winner_id=winner if isinstance(winner, str) else None,
        loser_id=loser if isinstance(loser, str) else None,
        error=error if isinstance(error, str) else None,
    )
