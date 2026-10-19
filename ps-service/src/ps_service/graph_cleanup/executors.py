"""Executors run when a signed passkey approval is verified.

`execute_capability_merge`, `execute_obligation_merge` and `execute_role_merge` are the
post-signature steps of `merge-capabilities`, `merge-obligations` and `merge-roles` (audit-first
and fail-closed). Order:

1. re-check the approving actor still holds `ComplianceOfficer` (a store outage denies);
2. re-read the graph, re-validate, and compare the state digest bound into the signed args;
   for a near-miss merge (a signed `review_id`) also check the `PendingReview` still exists and
   still names the pair (drift or a changed review means the previewed state is gone: nothing is
   written);
3. record the `applied` audit row, then write -- if the audit row cannot be recorded, no
   graph edit is made;
4. one gateway group, guarded by the graph's log position (all-or-nothing); a guard
   miss or failure records a `failed` row with the same `approval_id` (best effort) and returns a
   generic error.

A Capability merge refused at step 1 or 2 records a `failed` `capability.merge` row too (best
effort, no snapshots), with a `reason_code` naming the refusal, so the refusal is on the audit
trail even though nothing was written. Other tools' refusals before the write record no row.

The function never raises for an expected failure; every error outcome is a short message
with no internal detail. The module registers its executor and effect verifier with
`passkey_signing.executors` at import (same idiom as `register_audit_action`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from ps_service.api.errors import AuthorizationStoreUnavailableError
from ps_service.audit.errors import (
    AuditInvalidDetailsError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditUnknownActionError,
)
from ps_service.audit.store import EventIdAuditStore
from ps_service.authz.models import AccessRole
from ps_service.authz.service import check_role
from ps_service.graph_cleanup.audit_actions import (
    CAPABILITY_MERGE_ACTION,
    CAPABILITY_RELEASE_GOVERNANCE_ACTION,
    CAPABILITY_UNMERGE_ACTION,
    OBLIGATION_MERGE_ACTION,
    OBLIGATION_UNMERGE_ACTION,
    ROLE_MERGE_ACTION,
    ROLE_UNMERGE_ACTION,
    CapabilityMergeDetails,
    CapabilityReleaseGovernanceDetails,
    CapabilityUnmergeDetails,
    CleanupFailedReason,
    ObligationMergeDetails,
    ObligationUnmergeDetails,
    RoleMergeDetails,
    RoleUnmergeDetails,
)
from ps_service.graph_cleanup.dependencies import build_default_graph_cleanup_dependencies
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_capability_merged,
    read_capability_present_without_marker,
    read_governed_by_edge_present,
    read_obligation_marker,
    read_obligation_marker_present,
    read_obligation_merge_state,
    read_obligation_present,
    read_pending_review,
    read_release_state,
    read_role_merge_state,
    read_role_merged,
    read_role_present_without_marker,
)
from ps_service.graph_cleanup.graph_submission import graph_write_target
from ps_service.graph_cleanup.group_builders import (
    write_capability_merge,
    write_capability_unmerge,
    write_obligation_merge,
    write_obligation_unmerge,
    write_release,
    write_role_merge,
    write_role_unmerge,
)
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.graph_cleanup.role_planner import plan_role_merge, validate_role_review
from ps_service.graph_cleanup.unmerge import plan_unmerge
from ps_service.logging import emit_log_entry
from ps_service.passkey_signing.executors import (
    register_approval_executor,
    register_effect_verifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.graph_cleanup.graph_submission import GraphWriteTarget
    from ps_service.graph_cleanup.models import (
        CapabilityMergePlan,
        CapabilityUnmergePlan,
        MergeCase,
        ObligationMergePlan,
        ObligationUnmergePlan,
        ReleasePlan,
        RoleMergePlan,
        RoleUnmergePlan,
    )
    from ps_service.graph_cleanup.unmerge import UnmergePlan
    from ps_service.passkey_signing.models import PendingApprovalRow

__all__ = [
    "TOOL_MERGE_CAPABILITIES",
    "TOOL_MERGE_OBLIGATIONS",
    "TOOL_MERGE_ROLES",
    "TOOL_RELEASE_GOVERNANCE",
    "TOOL_UNMERGE",
    "execute_capability_merge",
    "execute_obligation_merge",
    "execute_release_governance",
    "execute_role_merge",
    "execute_unmerge",
    "verify_capability_merge_effect",
    "verify_obligation_merge_effect",
    "verify_release_governance_effect",
    "verify_role_merge_effect",
    "verify_unmerge_effect",
]

TOOL_MERGE_CAPABILITIES = "merge-capabilities"
TOOL_MERGE_OBLIGATIONS = "merge-obligations"
TOOL_MERGE_ROLES = "merge-roles"
TOOL_RELEASE_GOVERNANCE = "release-capability-governance"
TOOL_UNMERGE = "unmerge"
_COMPONENT = "graph_cleanup"
_AUDIT_ERRORS = (
    AuditPostgresUnavailableError,
    AuditPersistenceError,
    AuditUnknownActionError,
    AuditInvalidDetailsError,
)

_NOT_AUTHORISED = (
    "this action could not be completed; the approver no longer holds the required role"
)
_STALE = "the graph changed since the preview; nothing was changed, ask for a new approval"
_NOT_AUDITED = "the merge was not performed because it could not be audited; nothing was changed"
_WRITE_FAILED = (
    "the merge could not be completed; check its status with check-cleanup-approval before retrying"
)
_BAD_APPROVAL = "this approval could not be executed; ask for a new approval"
_ACK_REQUIRED = (
    "the governance change was not acknowledged in this approval; nothing was changed, "
    "ask for a new approval with acknowledge_governance_change"
)
_RELEASE_NOT_AUDITED = (
    "the release was not performed because it could not be audited; nothing was changed"
)
_RELEASE_WRITE_FAILED = (
    "the release could not be completed; "
    "check its status with check-cleanup-approval before retrying"
)
_UNAVAILABLE = "the policy graph database could not be reached; nothing was changed"
_AUDIT_UNREADABLE = "the audit trail could not be read; nothing was changed"
_UNMERGE_NOT_AUDITED = (
    "the unmerge was not performed because it could not be audited; nothing was changed"
)
_UNMERGE_WRITE_FAILED = (
    "the unmerge could not be completed; "
    "check its status with check-cleanup-approval before retrying"
)


def _error(message: str) -> dict[str, object]:
    return {"error": message}


def _log(row: PendingApprovalRow, outcome: str, reason: str | None = None) -> None:
    extra: dict[str, object] = {"approval_id": row.id, "tool_name": row.tool_name}
    if reason is not None:
        extra["reason"] = reason
    action = {
        TOOL_MERGE_OBLIGATIONS: "execute_obligation_merge",
        TOOL_MERGE_ROLES: "execute_role_merge",
        TOOL_RELEASE_GOVERNANCE: "execute_release_governance",
        TOOL_UNMERGE: "execute_unmerge",
    }.get(row.tool_name, "execute_capability_merge")
    emit_log_entry(component=_COMPONENT, action=action, outcome=outcome, extra=extra)


def _string_arg(row: PendingApprovalRow, name: str) -> str | None:
    value = row.normalized_args.get(name)
    return value if isinstance(value, str) else None


def _review_id(row: PendingApprovalRow) -> str | None:
    """The near-miss review id signed into a `merge-capabilities` or `merge-roles` approval."""
    return _string_arg(row, "review_id")


def _details(
    row: PendingApprovalRow,
    *,
    survivor_id: str,
    absorbed_id: str,
    plan: CapabilityMergePlan,
    failed_reason: Literal["graph_guard_missed", "graph_write_failed"] | None = None,
) -> dict[str, object]:
    governance = plan.preview.governance
    return CapabilityMergeDetails(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        policy_case=plan.preview.policy_case,
        acknowledged=row.normalized_args.get("acknowledge_governance_change") is True,
        approval_id=row.id,
        before=plan.before,
        after=plan.after,
        reason_code=failed_reason,
        review_id=_review_id(row),
        policy_id=governance.policy.id if governance else None,
        policy_status=governance.policy.status if governance else None,
        governed_set_before=governance.governed_set_before if governance else None,
        governed_set_after=governance.governed_set_after if governance else None,
    ).model_dump()


def _record(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    outcome: Literal["applied", "failed"],
    absorbed_id: str,
    details: dict[str, object],
    action: str = CAPABILITY_MERGE_ACTION,
    resource_type: str = "capability",
) -> None:
    dependencies.audit_store(config).record_standalone(
        actor_subject=row.actor_subject,
        actor_issuer=row.actor_issuer,
        action=action,
        resource_type=resource_type,
        resource_id=absorbed_id,
        outcome=outcome,
        details=details,
    )


def _record_applied(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    absorbed_id: str,
    details: dict[str, object],
    action: str,
    resource_type: str = "capability",
) -> str:
    """Record the `applied` row and return its id, the audit link of the group that follows.

    The audit store must report the id of the row it wrote (`EventIdAuditStore`, the real store
    does); a store that cannot is a wiring error, not a runtime condition, so a `TypeError`.
    """
    store = dependencies.audit_store(config)
    if not isinstance(store, EventIdAuditStore):
        message = "the audit store cannot report the id of the row it records"
        raise TypeError(message)
    return store.record_standalone_with_event_id(
        actor_subject=row.actor_subject,
        actor_issuer=row.actor_issuer,
        action=action,
        resource_type=resource_type,
        resource_id=absorbed_id,
        outcome="applied",
        details=details,
    )


def _display_policy_case(row: PendingApprovalRow) -> MergeCase:
    case = row.display_summary.get("policy_case")
    if case == 2:  # noqa: PLR2004 -- the Policy cases are named by number across the component
        return 2
    return 3 if case == 3 else 1  # noqa: PLR2004


def _refusal_details(
    row: PendingApprovalRow,
    *,
    survivor_id: str,
    absorbed_id: str,
    reason: CleanupFailedReason,
) -> dict[str, object]:
    """Details of a `failed` `capability.merge` row for a refusal made before any plan exists.

    No plan means no snapshots and no governing-Policy fields; the Policy case is the one the
    preview put in the approval's display summary (the orphan reconciler's convention), 1 when
    absent, and the acknowledgment is the signed one.
    """
    case = _display_policy_case(row)
    return CapabilityMergeDetails(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        policy_case=case,
        acknowledged=row.normalized_args.get("acknowledge_governance_change") is True,
        approval_id=row.id,
        reason_code=reason,
        review_id=_review_id(row),
    ).model_dump()


def _role_refusal_details(
    row: PendingApprovalRow,
    *,
    survivor_id: str,
    absorbed_id: str,
    reason: CleanupFailedReason,
) -> dict[str, object]:
    """Details of a `failed` `role.merge` row for a refusal made before any plan exists.

    No plan means no snapshots and no moved-Obligation fields; the instrument is the one the
    preview put in the approval's display summary (the orphan reconciler's convention).
    """
    return RoleMergeDetails(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        approval_id=row.id,
        instrument_id=str(row.display_summary.get("instrument_id", "")),
        reason_code=reason,
        review_id=_review_id(row),
    ).model_dump()


@dataclass(frozen=True)
class _Refusal:
    """Why and how a merge is refused: the reason code, the message returned, the log detail."""

    reason: CleanupFailedReason
    message: str
    log_detail: str | None = None


@dataclass(frozen=True)
class _RefusalKind:
    """Which cleanup action a refusal's `failed` row belongs to and how its details are built."""

    action: str
    resource_type: str
    details: Callable[..., dict[str, object]]


_CAPABILITY_REFUSAL = _RefusalKind(CAPABILITY_MERGE_ACTION, "capability", _refusal_details)
_ROLE_REFUSAL = _RefusalKind(ROLE_MERGE_ACTION, "role", _role_refusal_details)


def _refuse_as(
    kind: _RefusalKind,
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    survivor_id: str,
    absorbed_id: str,
    refusal: _Refusal,
) -> dict[str, object]:
    """Refuse a merge before the audit-first write: log, record a `failed` row, return the message.

    The row is recorded under the kind's action and resource type with the details the kind
    builds. It is best effort: an audit outage is logged and never masks the refusal.
    """
    _log(row, "failed", refusal.log_detail or refusal.reason)
    try:
        _record(
            row,
            dependencies,
            config,
            outcome="failed",
            absorbed_id=absorbed_id,
            details=kind.details(
                row, survivor_id=survivor_id, absorbed_id=absorbed_id, reason=refusal.reason
            ),
            action=kind.action,
            resource_type=kind.resource_type,
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"failed_row_not_recorded:{type(exc).__name__}")
    return _error(refusal.message)


def _refuse(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    survivor_id: str,
    absorbed_id: str,
    reason: CleanupFailedReason,
    message: str,
    log_detail: str | None = None,
) -> dict[str, object]:
    """Refuse a Capability merge before the audit-first write: a `failed` `capability.merge` row."""
    return _refuse_as(
        _CAPABILITY_REFUSAL,
        row,
        dependencies,
        config,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        refusal=_Refusal(reason, message, log_detail),
    )


def _actor_is_still_authorised(
    row: PendingApprovalRow, dependencies: GraphCleanupDependencies, config: ServiceConfig
) -> bool:
    try:
        return check_role(
            (row.actor_subject, row.actor_issuer),
            minimum=AccessRole.COMPLIANCE_OFFICER,
            store=dependencies.access_role_store(config),
        ).allowed
    except AuthorizationStoreUnavailableError:
        return False


def _write_after_audit(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    graph: GraphHandle,
    *,
    survivor_id: str,
    absorbed_id: str,
    plan: CapabilityMergePlan,
) -> dict[str, object]:
    """Steps 3-4: record `applied`, then one gateway group; on failure record `failed`.

    The group carries the `applied` row's id as its audit link. A group committed but not applied
    yet is a success (no `failed` row).
    """
    try:
        audit_event_id = _record_applied(
            row,
            dependencies,
            config,
            absorbed_id=absorbed_id,
            details=_details(row, survivor_id=survivor_id, absorbed_id=absorbed_id, plan=plan),
            action=CAPABILITY_MERGE_ACTION,
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_NOT_AUDITED)
    reason: Literal["graph_guard_missed", "graph_write_failed"]
    try:
        write_capability_merge(
            graph,
            graph_write_target(dependencies, config),
            audit_event_id=audit_event_id,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            expected=plan.expected,
            review_id=_review_id(row),
        )
    except GraphCleanupStaleStateError:
        reason, message = "graph_guard_missed", _STALE
    except GraphCleanupPersistenceError:
        reason, message = "graph_write_failed", _WRITE_FAILED
    else:
        _log(row, "succeeded")
        return {"survivor_id": survivor_id, "absorbed_id": absorbed_id, "merged": True}
    _log(row, "failed", reason)
    try:
        _record(
            row,
            dependencies,
            config,
            outcome="failed",
            absorbed_id=absorbed_id,
            details=_details(
                row,
                survivor_id=survivor_id,
                absorbed_id=absorbed_id,
                plan=plan,
                failed_reason=reason,
            ),
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"failed_row_not_recorded:{type(exc).__name__}")
    return _error(message)


def _graph_unavailable(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    survivor_id: str,
    absorbed_id: str,
    exc: Exception,
) -> dict[str, object]:
    """Refuse because the graph could not be read: the fixed message, a `failed` row.

    Only the exception type reaches the log; neither it nor its text reaches the outcome or the
    audit row.
    """
    return _refuse(
        row,
        dependencies,
        config,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        reason="graph_unavailable",
        message=_UNAVAILABLE,
        log_detail=f"graph_unavailable:{type(exc).__name__}",
    )


def _review_still_names_pair(
    graph: GraphHandle, review_id: str, *, survivor_id: str, absorbed_id: str
) -> bool:
    """Whether the near-miss `PendingReview` still exists and still names this Capability pair."""
    from ps_service.company_merge.errors import (  # noqa: PLC0415 -- function-local, as above
        UnknownReviewKindError,
    )
    from ps_service.company_merge.pending_review import (  # noqa: PLC0415 -- function-local keeps graph_cleanup off Company Merge at import
        get_pending_review,
    )
    from ps_service.company_merge.reviewable_kinds import (  # noqa: PLC0415 -- function-local, as above
        CAPABILITY,
    )

    try:
        review = get_pending_review(graph, review_id)
    except UnknownReviewKindError:
        return False  # a review whose kind is no longer understood no longer names this pair
    return (
        review is not None
        and review.kind == CAPABILITY.label
        and {review.incoming_id, review.nearest_existing_id} == {survivor_id, absorbed_id}
    )


def _stale_refusal(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    survivor_id: str,
    absorbed_id: str,
    plan: CapabilityMergePlan,
    graph: GraphHandle,
    digest: str,
) -> dict[str, object] | None:
    """The refusal outcome when the previewed state no longer holds, else None.

    Checks, in order: the signed acknowledgment (defence in depth), the state digest, and that the
    near-miss review the approval resolves still names this pair.
    """
    refusal: tuple[CleanupFailedReason, str] | None = None
    governance = plan.preview.governance
    review_id = _review_id(row)
    if (
        governance is not None
        and governance.acknowledgment_required
        and row.normalized_args.get("acknowledge_governance_change") is not True
    ):
        # Defence in depth: the approval flow never creates such a row.
        refusal = ("acknowledgment_missing", _ACK_REQUIRED)
    elif plan.state_digest != digest:
        refusal = ("state_digest_mismatch", _STALE)
    elif review_id is not None and not _review_still_names_pair(
        graph, review_id, survivor_id=survivor_id, absorbed_id=absorbed_id
    ):
        refusal = ("review_missing_or_changed", _STALE)
    if refusal is None:
        return None
    reason, message = refusal
    return _refuse(
        row,
        dependencies,
        config,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        reason=reason,
        message=message,
    )


def _replan_capability_merge(
    row: PendingApprovalRow,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
    *,
    survivor_id: str,
    absorbed_id: str,
    digest: str,
) -> tuple[GraphHandle, CapabilityMergePlan] | dict[str, object]:
    """Step 2: re-read and re-validate; the plan, or the error outcome to return.

    Every refusal here is a stale or rejected state, and records a `failed` row.
    """
    try:
        graph = dependencies.open_single_tenant_graph(config)
        state = read_capability_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
        plan = plan_capability_merge(state)
    except GraphCleanupValidationError as exc:
        return _refuse(
            row,
            dependencies,
            config,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            reason="revalidation_rejected",
            message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 -- opening/reading the graph may fail in driver-specific ways; the message returned never carries them
        return _graph_unavailable(
            row, dependencies, config, survivor_id=survivor_id, absorbed_id=absorbed_id, exc=exc
        )
    try:
        refusal = _stale_refusal(
            row,
            dependencies,
            config,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            plan=plan,
            graph=graph,
            digest=digest,
        )
    except Exception as exc:  # noqa: BLE001 -- the review read may fail in driver-specific ways; the message returned never carries them
        return _graph_unavailable(
            row, dependencies, config, survivor_id=survivor_id, absorbed_id=absorbed_id, exc=exc
        )
    return refusal if refusal is not None else (graph, plan)


def execute_capability_merge(
    row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> dict[str, object]:
    """Execute one signed `merge-capabilities` approval; return its outcome dict.

    Applies the previewed merge only if the approver still holds `ComplianceOfficer`, the graph
    still matches the state digest in the signed arguments, and the audit row was recorded first.
    A merge where exactly one Capability is governed is refused unless the signed arguments
    carry `acknowledge_governance_change`. When the signed arguments carry a `review_id`, the
    near-miss `PendingReview` it names must still exist and name the pair, is deleted in the
    same group and the id is recorded on the audit row; a `review_id` that is not a string makes
    the approval malformed.

    Args:
        row: The verified approval; its `normalized_args` carry the signed arguments and its
            actor identifies who approved.
        config: Service configuration used to open the graph and the stores.
        dependencies: Factories for the graph, audit store, access-role store and gateway.

    Returns:
        On success `{"survivor_id", "absorbed_id", "merged": True}`. On any expected failure
        `{"error": <short message without internal detail>}`; this function does not raise for
        them (malformed arguments, lost authority, drift, unavailable graph or audit, failed
        write).

    Side Effects:
        Writes an `applied` `capability.merge` audit row, then one gateway group that deletes
        the absorbed Capability and leaves a `MergedCapability` marker. When the guard or the
        write fails, or the merge is refused before the write (lost authority, drift, a changed
        review, an unreachable graph), records a `failed` row with the same `approval_id` (best
        effort). Emits a structured log entry.
    """
    survivor_id = _string_arg(row, "survivor_id")
    absorbed_id = _string_arg(row, "absorbed_id")
    digest = _string_arg(row, "state_digest")
    review_is_malformed = "review_id" in row.normalized_args and _review_id(row) is None
    if survivor_id is None or absorbed_id is None or digest is None or review_is_malformed:
        _log(row, "failed", "malformed_normalized_args")
        return _error(_BAD_APPROVAL)
    if not _actor_is_still_authorised(row, dependencies, config):
        return _refuse(
            row,
            dependencies,
            config,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            reason="actor_not_authorised",
            message=_NOT_AUTHORISED,
        )
    replanned = _replan_capability_merge(
        row,
        config,
        dependencies,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        digest=digest,
    )
    if isinstance(replanned, dict):
        return replanned
    graph, plan = replanned
    return _write_after_audit(
        row,
        dependencies,
        config,
        graph,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        plan=plan,
    )


def verify_capability_merge_effect(row: PendingApprovalRow, graph: GraphHandle) -> bool:
    """Report whether the approved Capability merge is present in the graph.

    Used to settle an approval whose outcome is unknown.

    Args:
        row: The approval; needs string `survivor_id` and `absorbed_id` in `normalized_args`.
        graph: Handle on the single-tenant graph, read only.

    Returns:
        True when a `MergedCapability` marker names the survivor and no Capability has the
        absorbed id any more; False when not, or when either id is missing from the arguments.
    """
    survivor_id = _string_arg(row, "survivor_id")
    absorbed_id = _string_arg(row, "absorbed_id")
    if survivor_id is None or absorbed_id is None:
        return False
    return read_capability_merged(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)


def _obligation_details(
    row: PendingApprovalRow,
    *,
    survivor_id: str,
    absorbed_id: str,
    plan: ObligationMergePlan,
    failed_reason: Literal["graph_guard_missed", "graph_write_failed"] | None = None,
) -> dict[str, object]:
    return ObligationMergeDetails(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        role_id=plan.preview.role_id,
        approval_id=row.id,
        before=plan.before,
        after=plan.after,
        reason_code=failed_reason,
    ).model_dump()


def _record_obligation(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    outcome: Literal["applied", "failed"],
    absorbed_id: str,
    details: dict[str, object],
) -> None:
    _record(
        row,
        dependencies,
        config,
        outcome=outcome,
        absorbed_id=absorbed_id,
        details=details,
        action=OBLIGATION_MERGE_ACTION,
        resource_type="obligation",
    )


def _write_obligation_after_audit(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    graph: GraphHandle,
    *,
    survivor_id: str,
    absorbed_id: str,
    plan: ObligationMergePlan,
) -> dict[str, object]:
    """Steps 3-4 for an obligation merge: record `applied`, then one gateway group.

    The group carries the `applied` row's id as its audit link; `failed` is recorded on error. A
    group committed but not applied yet is a success (no `failed` row).
    """
    try:
        audit_event_id = _record_applied(
            row,
            dependencies,
            config,
            absorbed_id=absorbed_id,
            details=_obligation_details(
                row, survivor_id=survivor_id, absorbed_id=absorbed_id, plan=plan
            ),
            action=OBLIGATION_MERGE_ACTION,
            resource_type="obligation",
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_NOT_AUDITED)
    reason: Literal["graph_guard_missed", "graph_write_failed"]
    try:
        write_obligation_merge(
            graph,
            graph_write_target(dependencies, config),
            audit_event_id=audit_event_id,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            expected=plan.expected,
        )
    except GraphCleanupStaleStateError:
        reason, message = "graph_guard_missed", _STALE
    except GraphCleanupPersistenceError:
        reason, message = "graph_write_failed", _WRITE_FAILED
    else:
        _log(row, "succeeded")
        return {"survivor_id": survivor_id, "absorbed_id": absorbed_id, "merged": True}
    _log(row, "failed", reason)
    try:
        _record_obligation(
            row,
            dependencies,
            config,
            outcome="failed",
            absorbed_id=absorbed_id,
            details=_obligation_details(
                row,
                survivor_id=survivor_id,
                absorbed_id=absorbed_id,
                plan=plan,
                failed_reason=reason,
            ),
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"failed_row_not_recorded:{type(exc).__name__}")
    return _error(message)


def execute_obligation_merge(
    row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> dict[str, object]:
    """Execute one signed `merge-obligations` approval; return its outcome dict.

    Follows the same fail-closed order as `execute_capability_merge`: authority re-check,
    re-read and digest comparison, `applied` audit row, then one guarded gateway group. When the
    signed arguments carry a `review_id`, the Role `PendingReview` it names must still exist and
    name the pair, is deleted in the same group and the id is recorded on the audit row; a
    `review_id` that is not a string makes the approval malformed.

    Args:
        row: The verified approval; its `normalized_args` carry the signed arguments and its
            actor identifies who approved.
        config: Service configuration used to open the graph and the stores.
        dependencies: Factories for the graph, audit store, access-role store and gateway.

    Returns:
        On success `{"survivor_id", "absorbed_id", "merged": True}`. On any expected failure
        `{"error": <short message without internal detail>}`; this function does not raise for
        them.

    Side Effects:
        Writes an `applied` `obligation.merge` audit row, then one gateway group that deletes
        the absorbed Obligation and leaves a marker. When the guard or the write fails, records a
        `failed` row with the same `approval_id` (best effort). Emits a structured log entry.
    """
    survivor_id = _string_arg(row, "survivor_id")
    absorbed_id = _string_arg(row, "absorbed_id")
    digest = _string_arg(row, "state_digest")
    if survivor_id is None or absorbed_id is None or digest is None:
        _log(row, "failed", "malformed_normalized_args")
        return _error(_BAD_APPROVAL)
    if not _actor_is_still_authorised(row, dependencies, config):
        _log(row, "failed", "actor_not_authorised")
        return _error(_NOT_AUTHORISED)
    try:
        graph = dependencies.open_single_tenant_graph(config)
        state = read_obligation_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
        plan = plan_obligation_merge(state)
    except GraphCleanupValidationError as exc:
        _log(row, "failed", "revalidation_rejected")
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 -- opening/reading the graph may fail in driver-specific ways; the message returned never carries them
        _log(row, "failed", f"graph_unavailable:{type(exc).__name__}")
        return _error(_UNAVAILABLE)
    if plan.state_digest != digest:
        _log(row, "failed", "state_digest_mismatch")
        return _error(_STALE)
    return _write_obligation_after_audit(
        row,
        dependencies,
        config,
        graph,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        plan=plan,
    )


def verify_obligation_merge_effect(row: PendingApprovalRow, graph: GraphHandle) -> bool:
    """Report whether the approved Obligation merge is present in the graph.

    Args:
        row: The approval; needs string `survivor_id` and `absorbed_id` in `normalized_args`.
        graph: Handle on the single-tenant graph, read only.

    Returns:
        True when the absorbed Obligation no longer exists and its merge marker is present; False
        otherwise, or when either id is missing from the arguments.
    """
    survivor_id = _string_arg(row, "survivor_id")
    absorbed_id = _string_arg(row, "absorbed_id")
    if survivor_id is None or absorbed_id is None:
        return False
    return not read_obligation_present(graph, obligation_id=absorbed_id) and read_obligation_marker(
        graph, survivor_id=survivor_id, absorbed_id=absorbed_id
    )


def _role_details(
    row: PendingApprovalRow,
    *,
    survivor_id: str,
    absorbed_id: str,
    plan: RoleMergePlan,
    failed_reason: Literal["graph_guard_missed", "graph_write_failed"] | None = None,
) -> dict[str, object]:
    return RoleMergeDetails(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        approval_id=row.id,
        instrument_id=plan.preview.instrument_id,
        defines_source_ref=plan.absorbed_defines_source_ref,
        moved_obligation_ids=plan.preview.moved_obligation_ids,
        survivor_has_count_after_merge=plan.survivor_has_count_after_merge,
        before=plan.before,
        after=plan.after,
        reason_code=failed_reason,
        review_id=_review_id(row),
    ).model_dump()


def _record_role(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    outcome: Literal["applied", "failed"],
    absorbed_id: str,
    details: dict[str, object],
) -> None:
    _record(
        row,
        dependencies,
        config,
        outcome=outcome,
        absorbed_id=absorbed_id,
        details=details,
        action=ROLE_MERGE_ACTION,
        resource_type="role",
    )


def _write_role_after_audit(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    graph: GraphHandle,
    *,
    survivor_id: str,
    absorbed_id: str,
    plan: RoleMergePlan,
) -> dict[str, object]:
    """Steps 3-4 for a role merge: record `applied`, then one gateway group.

    The group carries the `applied` row's id as its audit link; `failed` is recorded on error. A
    group committed but not applied yet is a success (no `failed` row).
    """
    try:
        audit_event_id = _record_applied(
            row,
            dependencies,
            config,
            absorbed_id=absorbed_id,
            details=_role_details(row, survivor_id=survivor_id, absorbed_id=absorbed_id, plan=plan),
            action=ROLE_MERGE_ACTION,
            resource_type="role",
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_NOT_AUDITED)
    reason: Literal["graph_guard_missed", "graph_write_failed"]
    try:
        write_role_merge(
            graph,
            graph_write_target(dependencies, config),
            audit_event_id=audit_event_id,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            expected=plan.expected,
            review_id=_review_id(row),
        )
    except GraphCleanupStaleStateError:
        reason, message = "graph_guard_missed", _STALE
    except GraphCleanupPersistenceError:
        reason, message = "graph_write_failed", _WRITE_FAILED
    else:
        _log(row, "succeeded")
        return {"survivor_id": survivor_id, "absorbed_id": absorbed_id, "merged": True}
    _log(row, "failed", reason)
    try:
        _record_role(
            row,
            dependencies,
            config,
            outcome="failed",
            absorbed_id=absorbed_id,
            details=_role_details(
                row,
                survivor_id=survivor_id,
                absorbed_id=absorbed_id,
                plan=plan,
                failed_reason=reason,
            ),
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"failed_row_not_recorded:{type(exc).__name__}")
    return _error(message)


def _refuse_role(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    survivor_id: str,
    absorbed_id: str,
    reason: CleanupFailedReason,
    message: str,
    log_detail: str | None = None,
) -> dict[str, object]:
    """Refuse a Role merge before the audit-first write: a `failed` `role.merge` row."""
    return _refuse_as(
        _ROLE_REFUSAL,
        row,
        dependencies,
        config,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        refusal=_Refusal(reason, message, log_detail),
    )


def _role_review_holds(row: PendingApprovalRow, graph: GraphHandle, plan: RoleMergePlan) -> bool:
    """Whether the signed review, if any, still exists, is a Role review and names this pair."""
    review_id = _review_id(row)
    if review_id is None:
        return True
    try:
        validate_role_review(
            read_pending_review(graph, review_id=review_id),
            survivor_id=plan.preview.survivor_id,
            absorbed_id=plan.preview.absorbed_id,
        )
    except GraphCleanupValidationError:
        return False
    return True


def _replan_role_merge(
    row: PendingApprovalRow,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
    *,
    survivor_id: str,
    absorbed_id: str,
    digest: str,
) -> tuple[GraphHandle, RoleMergePlan] | dict[str, object]:
    """Step 2 for a role merge: re-read, re-validate and compare the digest.

    Returns the graph and plan, or the error outcome to return. Every refusal here is a stale or
    rejected state, an unreachable graph included, and records a `failed` `role.merge` row.
    """
    try:
        graph = dependencies.open_single_tenant_graph(config)
        state = read_role_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
        plan = plan_role_merge(state)
        review_holds = _role_review_holds(row, graph, plan)
    except GraphCleanupValidationError as exc:
        return _refuse_role(
            row,
            dependencies,
            config,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            reason="revalidation_rejected",
            message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 -- opening/reading the graph may fail in driver-specific ways; the message returned never carries them
        return _refuse_role(
            row,
            dependencies,
            config,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            reason="graph_unavailable",
            message=_UNAVAILABLE,
            log_detail=f"graph_unavailable:{type(exc).__name__}",
        )
    if plan.state_digest != digest:
        return _refuse_role(
            row,
            dependencies,
            config,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            reason="state_digest_mismatch",
            message=_STALE,
        )
    if not review_holds:
        return _refuse_role(
            row,
            dependencies,
            config,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            reason="review_missing_or_changed",
            message=_STALE,
        )
    return graph, plan


def execute_role_merge(
    row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> dict[str, object]:
    """Execute one signed `merge-roles` approval; return its outcome dict.

    Follows the same fail-closed order as `execute_capability_merge`: authority re-check,
    re-read and digest comparison, `applied` audit row, then one guarded gateway group.

    Args:
        row: The verified approval; its `normalized_args` carry the signed arguments and its
            actor identifies who approved.
        config: Service configuration used to open the graph and the stores.
        dependencies: Factories for the graph, audit store, access-role store and gateway.

    Returns:
        On success `{"survivor_id", "absorbed_id", "merged": True}`. On any expected failure
        `{"error": <short message without internal detail>}`; this function does not raise for
        them.

    Side Effects:
        Writes an `applied` `role.merge` audit row, then one gateway group that moves the absorbed
        Role's Obligations to the survivor, deletes the absorbed Role and leaves a `MergedRole`
        marker. When the guard or the write fails, or the merge is refused before the write (lost
        authority, drift, a Role that no longer validates, an unreachable graph), records a
        `failed` row with the same `approval_id` (best effort). Emits a structured log entry.
    """
    survivor_id = _string_arg(row, "survivor_id")
    absorbed_id = _string_arg(row, "absorbed_id")
    digest = _string_arg(row, "state_digest")
    review_is_malformed = "review_id" in row.normalized_args and _review_id(row) is None
    if survivor_id is None or absorbed_id is None or digest is None or review_is_malformed:
        _log(row, "failed", "malformed_normalized_args")
        return _error(_BAD_APPROVAL)
    if not _actor_is_still_authorised(row, dependencies, config):
        return _refuse_role(
            row,
            dependencies,
            config,
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            reason="actor_not_authorised",
            message=_NOT_AUTHORISED,
        )
    replanned = _replan_role_merge(
        row, config, dependencies, survivor_id=survivor_id, absorbed_id=absorbed_id, digest=digest
    )
    if isinstance(replanned, dict):
        return replanned
    graph, plan = replanned
    return _write_role_after_audit(
        row,
        dependencies,
        config,
        graph,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        plan=plan,
    )


def verify_role_merge_effect(row: PendingApprovalRow, graph: GraphHandle) -> bool:
    """Report whether the approved Role merge is present in the graph.

    Args:
        row: The approval; needs string `survivor_id` and `absorbed_id` in `normalized_args`.
        graph: Handle on the single-tenant graph, read only.

    Returns:
        True when the absorbed Role no longer exists and a `MergedRole` marker names the
        survivor; False otherwise, or when either id is missing from the arguments.
    """
    survivor_id = _string_arg(row, "survivor_id")
    absorbed_id = _string_arg(row, "absorbed_id")
    if survivor_id is None or absorbed_id is None:
        return False
    return read_role_merged(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)


def _release_details(
    row: PendingApprovalRow,
    *,
    plan: ReleasePlan,
    failed_reason: Literal["graph_guard_missed", "graph_write_failed"] | None = None,
) -> dict[str, object]:
    preview = plan.preview
    return CapabilityReleaseGovernanceDetails(
        capability_id=preview.capability_id,
        policy_id=preview.policy_id,
        policy_status=preview.policy_status,
        approval_id=row.id,
        before=plan.before,
        after=plan.after,
        governed_set_before=preview.governed_set_before,
        governed_set_after=preview.governed_set_after,
        reason_code=failed_reason,
    ).model_dump()


def _record_release(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    outcome: Literal["applied", "failed"],
    plan: ReleasePlan,
    failed_reason: Literal["graph_guard_missed", "graph_write_failed"] | None = None,
) -> None:
    _record(
        row,
        dependencies,
        config,
        outcome=outcome,
        absorbed_id=plan.preview.capability_id,
        details=_release_details(row, plan=plan, failed_reason=failed_reason),
        action=CAPABILITY_RELEASE_GOVERNANCE_ACTION,
        resource_type="capability",
    )


def _write_release_after_audit(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    graph: GraphHandle,
    *,
    plan: ReleasePlan,
) -> dict[str, object]:
    """Steps 3-4 for a release: record `applied`, then one gateway group; `failed` on error.

    The group deletes the `GOVERNED_BY` edge and carries the `applied` row's id as its audit link.
    A group committed but not applied yet is a success (no `failed` row).
    """
    try:
        audit_event_id = _record_applied(
            row,
            dependencies,
            config,
            absorbed_id=plan.preview.capability_id,
            details=_release_details(row, plan=plan),
            action=CAPABILITY_RELEASE_GOVERNANCE_ACTION,
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_RELEASE_NOT_AUDITED)
    reason: Literal["graph_guard_missed", "graph_write_failed"]
    try:
        write_release(
            graph,
            graph_write_target(dependencies, config),
            audit_event_id=audit_event_id,
            capability_id=plan.preview.capability_id,
            policy_id=plan.preview.policy_id,
        )
    except GraphCleanupStaleStateError:
        reason, message = "graph_guard_missed", _STALE
    except GraphCleanupPersistenceError:
        reason, message = "graph_write_failed", _RELEASE_WRITE_FAILED
    else:
        _log(row, "succeeded")
        return {
            "capability_id": plan.preview.capability_id,
            "policy_id": plan.preview.policy_id,
            "released": True,
        }
    _log(row, "failed", reason)
    try:
        _record_release(
            row, dependencies, config, outcome="failed", plan=plan, failed_reason=reason
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"failed_row_not_recorded:{type(exc).__name__}")
    return _error(message)


def execute_release_governance(
    row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> dict[str, object]:
    """Execute one signed `release-capability-governance` approval; return its outcome dict.

    Removes the `GOVERNED_BY` edge between a Capability and its draft policy, following the same
    fail-closed order as the merges. The approval is refused as stale when the state digest or
    the policy id no longer matches the graph.

    Args:
        row: The verified approval; its `normalized_args` carry the signed arguments and its
            actor identifies who approved.
        config: Service configuration used to open the graph and the stores.
        dependencies: Factories for the graph, audit store, access-role store and gateway.

    Returns:
        On success `{"capability_id", "policy_id", "released": True}`. On any expected failure
        `{"error": <short message without internal detail>}`; this function does not raise for
        them.

    Side Effects:
        Writes an `applied` `capability.release_governance` audit row, then one gateway group
        that deletes the edge. When the guard or the write fails, records a `failed` row with
        the same `approval_id` (best effort). Emits a structured log entry.
    """
    capability_id = _string_arg(row, "capability_id")
    policy_id = _string_arg(row, "policy_id")
    digest = _string_arg(row, "state_digest")
    if capability_id is None or policy_id is None or digest is None:
        _log(row, "failed", "malformed_normalized_args")
        return _error(_BAD_APPROVAL)
    if not _actor_is_still_authorised(row, dependencies, config):
        _log(row, "failed", "actor_not_authorised")
        return _error(_NOT_AUTHORISED)
    try:
        graph = dependencies.open_single_tenant_graph(config)
        plan = plan_release_governance(read_release_state(graph, capability_id=capability_id))
    except GraphCleanupValidationError as exc:
        _log(row, "failed", "revalidation_rejected")
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 -- opening/reading the graph may fail in driver-specific ways; the message returned never carries them
        _log(row, "failed", f"graph_unavailable:{type(exc).__name__}")
        return _error(_UNAVAILABLE)
    if plan.state_digest != digest or plan.preview.policy_id != policy_id:
        _log(row, "failed", "state_digest_mismatch")
        return _error(_STALE)
    return _write_release_after_audit(row, dependencies, config, graph, plan=plan)


def verify_release_governance_effect(row: PendingApprovalRow, graph: GraphHandle) -> bool:
    """Report whether the approved governance release is present in the graph.

    Args:
        row: The approval; needs string `capability_id` and `policy_id` in `normalized_args`.
        graph: Handle on the single-tenant graph, read only.

    Returns:
        True when no `GOVERNED_BY` edge links the Capability to the policy; False when it still
        exists, or when either id is missing from the arguments.
    """
    capability_id = _string_arg(row, "capability_id")
    policy_id = _string_arg(row, "policy_id")
    if capability_id is None or policy_id is None:
        return False
    return not read_governed_by_edge_present(
        graph, capability_id=capability_id, policy_id=policy_id
    )


def _execute_registered(row: PendingApprovalRow, config: ServiceConfig) -> dict[str, object]:
    return execute_capability_merge(row, config, build_default_graph_cleanup_dependencies())


def _execute_obligation_registered(
    row: PendingApprovalRow, config: ServiceConfig
) -> dict[str, object]:
    return execute_obligation_merge(row, config, build_default_graph_cleanup_dependencies())


register_approval_executor(TOOL_MERGE_CAPABILITIES, _execute_registered)
register_effect_verifier(TOOL_MERGE_CAPABILITIES, verify_capability_merge_effect)
register_approval_executor(TOOL_MERGE_OBLIGATIONS, _execute_obligation_registered)
register_effect_verifier(TOOL_MERGE_OBLIGATIONS, verify_obligation_merge_effect)


def _execute_release_registered(
    row: PendingApprovalRow, config: ServiceConfig
) -> dict[str, object]:
    return execute_release_governance(row, config, build_default_graph_cleanup_dependencies())


def _execute_role_registered(row: PendingApprovalRow, config: ServiceConfig) -> dict[str, object]:
    return execute_role_merge(row, config, build_default_graph_cleanup_dependencies())


register_approval_executor(TOOL_MERGE_ROLES, _execute_role_registered)
register_effect_verifier(TOOL_MERGE_ROLES, verify_role_merge_effect)


register_approval_executor(TOOL_RELEASE_GOVERNANCE, _execute_release_registered)
register_effect_verifier(TOOL_RELEASE_GOVERNANCE, verify_release_governance_effect)


type _UnmergeFailedReason = Literal["graph_guard_missed", "graph_write_failed"]


def _capability_unmerge_details(
    row: PendingApprovalRow, plan: UnmergePlan, failed_reason: _UnmergeFailedReason | None
) -> dict[str, object]:
    capability = cast("CapabilityUnmergePlan", plan)
    return CapabilityUnmergeDetails(
        survivor_id=capability.preview.survivor_id,
        absorbed_id=capability.preview.merged_id,
        approval_id=row.id,
        reverses_approval_id=capability.preview.merge_approval_id,
        policy_case=capability.inputs.policy_case,
        before=capability.before,
        after=capability.after,
        restored_edges=capability.restored_edges,
        survivor_added_edges=capability.preview.survivor_added_edges,
        reason_code=failed_reason,
    ).model_dump()


def _obligation_unmerge_details(
    row: PendingApprovalRow, plan: UnmergePlan, failed_reason: _UnmergeFailedReason | None
) -> dict[str, object]:
    obligation = cast("ObligationUnmergePlan", plan)
    return ObligationUnmergeDetails(
        survivor_id=obligation.preview.survivor_id,
        absorbed_id=obligation.preview.merged_id,
        role_id=obligation.inputs.role_id,
        approval_id=row.id,
        reverses_approval_id=obligation.preview.merge_approval_id,
        before=obligation.before,
        after=obligation.after,
        restored_edges=obligation.restored_edges,
        survivor_added_edges=obligation.preview.survivor_added_edges,
        survivor_edges_possibly_from_merge=obligation.preview.survivor_edges_possibly_from_merge,
        reason_code=failed_reason,
    ).model_dump()


def _role_unmerge_details(
    row: PendingApprovalRow, plan: UnmergePlan, failed_reason: _UnmergeFailedReason | None
) -> dict[str, object]:
    role = cast("RoleUnmergePlan", plan)
    return RoleUnmergeDetails(
        survivor_id=role.preview.survivor_id,
        absorbed_id=role.preview.merged_id,
        approval_id=row.id,
        reverses_approval_id=role.preview.merge_approval_id,
        instrument_id=role.preview.instrument_id,
        before=role.before,
        after=role.after,
        restored_obligation_ids=role.preview.obligations_to_restore,
        survivor_obligations_added_since_merge=role.preview.survivor_obligations_added_since_merge,
        reason_code=failed_reason,
    ).model_dump()


def _write_capability_unmerge(
    graph: GraphHandle, target: GraphWriteTarget, audit_event_id: str, plan: UnmergePlan
) -> None:
    write_capability_unmerge(
        graph, target, audit_event_id=audit_event_id, plan=cast("CapabilityUnmergePlan", plan)
    )


def _write_obligation_unmerge(
    graph: GraphHandle, target: GraphWriteTarget, audit_event_id: str, plan: UnmergePlan
) -> None:
    write_obligation_unmerge(
        graph, target, audit_event_id=audit_event_id, plan=cast("ObligationUnmergePlan", plan)
    )


def _write_role_unmerge(
    graph: GraphHandle, target: GraphWriteTarget, audit_event_id: str, plan: UnmergePlan
) -> None:
    write_role_unmerge(
        graph, target, audit_event_id=audit_event_id, plan=cast("RoleUnmergePlan", plan)
    )


def _edge_unmerge_outcome(plan: UnmergePlan) -> dict[str, object]:
    """The outcome of a Capability or Obligation unmerge: the edges the survivor gained stay."""
    preview = cast("CapabilityUnmergePlan | ObligationUnmergePlan", plan).preview
    return {
        "merged_id": preview.merged_id,
        "survivor_id": preview.survivor_id,
        "unmerged": True,
        "survivor_added_edges": [
            edge.model_dump(mode="json") for edge in preview.survivor_added_edges
        ],
    }


def _role_unmerge_outcome(plan: UnmergePlan) -> dict[str, object]:
    """The outcome of a Role unmerge: how many Obligations the survivor gained and keeps."""
    preview = cast("RoleUnmergePlan", plan).preview
    return {
        "merged_id": preview.merged_id,
        "survivor_id": preview.survivor_id,
        "unmerged": True,
        "survivor_obligations_added_since_merge": preview.survivor_obligations_added_since_merge,
    }


def _capability_unmerged(graph: GraphHandle, merged_id: str) -> bool:
    return read_capability_present_without_marker(graph, capability_id=merged_id)


def _obligation_unmerged(graph: GraphHandle, merged_id: str) -> bool:
    return read_obligation_present(
        graph, obligation_id=merged_id
    ) and not read_obligation_marker_present(graph, obligation_id=merged_id)


def _role_unmerged(graph: GraphHandle, merged_id: str) -> bool:
    return read_role_present_without_marker(graph, role_id=merged_id)


@dataclass(frozen=True)
class _UnmergeKind:
    """What differs between unmerging a Capability, an Obligation and a Role merge.

    `details` builds the audit row's details (the optional reason makes it a `failed` row), `write`
    runs the one gateway group, `outcome` is the success result and `verify` the effect check the
    orphan reconciler uses.
    """

    action: str
    resource_type: str
    details: Callable[
        [PendingApprovalRow, UnmergePlan, _UnmergeFailedReason | None], dict[str, object]
    ]
    write: Callable[[GraphHandle, GraphWriteTarget, str, UnmergePlan], None]
    outcome: Callable[[UnmergePlan], dict[str, object]]
    verify: Callable[[GraphHandle, str], bool]


_CAPABILITY_UNMERGE = _UnmergeKind(
    CAPABILITY_UNMERGE_ACTION,
    "capability",
    _capability_unmerge_details,
    _write_capability_unmerge,
    _edge_unmerge_outcome,
    _capability_unmerged,
)
# Keyed by the signed unmerge `kind`, which is the located merge's kind.
_UNMERGE_KINDS: dict[str, _UnmergeKind] = {
    "capability": _CAPABILITY_UNMERGE,
    "obligation": _UnmergeKind(
        OBLIGATION_UNMERGE_ACTION,
        "obligation",
        _obligation_unmerge_details,
        _write_obligation_unmerge,
        _edge_unmerge_outcome,
        _obligation_unmerged,
    ),
    "role": _UnmergeKind(
        ROLE_UNMERGE_ACTION,
        "role",
        _role_unmerge_details,
        _write_role_unmerge,
        _role_unmerge_outcome,
        _role_unmerged,
    ),
}


def _role_unmerge_refusal_details(
    row: PendingApprovalRow,
    *,
    survivor_id: str,
    absorbed_id: str,
    reason: CleanupFailedReason,
) -> dict[str, object]:
    """Details of a `failed` `role.unmerge` row for a refusal made before any plan exists.

    No plan means no snapshots and no restored set; the survivor and instrument are the ones the
    preview put in the approval's display summary (the orphan reconciler's convention).
    """
    return RoleUnmergeDetails(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        approval_id=row.id,
        reverses_approval_id=str(row.normalized_args.get("merge_approval_id", "")),
        instrument_id=str(row.display_summary.get("instrument_id", "")) or None,
        reason_code=reason,
    ).model_dump()


_ROLE_UNMERGE_REFUSAL = _RefusalKind(ROLE_UNMERGE_ACTION, "role", _role_unmerge_refusal_details)


def _refuse_unmerge(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    kind: str,
    merged_id: str,
    refusal: _Refusal,
) -> dict[str, object]:
    """Refuse an unmerge before the audit-first write: log, return the message.

    A Role unmerge also records a `failed` `role.unmerge` row (best effort); a Capability or
    Obligation unmerge records none.
    """
    if kind != "role":
        _log(row, "failed", refusal.log_detail or refusal.reason)
        return _error(refusal.message)
    return _refuse_as(
        _ROLE_UNMERGE_REFUSAL,
        row,
        dependencies,
        config,
        survivor_id=str(row.display_summary.get("survivor_id", "")),
        absorbed_id=merged_id,
        refusal=refusal,
    )


def _record_unmerge_failed(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    plan: UnmergePlan,
    failed_reason: _UnmergeFailedReason,
) -> None:
    kind = _UNMERGE_KINDS[plan.preview.kind]
    _record(
        row,
        dependencies,
        config,
        outcome="failed",
        absorbed_id=plan.preview.merged_id,
        details=kind.details(row, plan, failed_reason),
        action=kind.action,
        resource_type=kind.resource_type,
    )


def _write_unmerge_after_audit(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    graph: GraphHandle,
    *,
    plan: UnmergePlan,
) -> dict[str, object]:
    """Steps 3-4 for an unmerge: record `applied`, then one gateway group; `failed` on error.

    The group carries the `applied` row's id as its audit link. A group committed but not applied
    yet is a success (no `failed` row).
    """
    kind = _UNMERGE_KINDS[plan.preview.kind]
    try:
        audit_event_id = _record_applied(
            row,
            dependencies,
            config,
            absorbed_id=plan.preview.merged_id,
            details=kind.details(row, plan, None),
            action=kind.action,
            resource_type=kind.resource_type,
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_UNMERGE_NOT_AUDITED)
    reason: _UnmergeFailedReason
    try:
        kind.write(graph, graph_write_target(dependencies, config), audit_event_id, plan)
    except GraphCleanupStaleStateError:
        reason, message = "graph_guard_missed", _STALE
    except GraphCleanupPersistenceError:
        reason, message = "graph_write_failed", _UNMERGE_WRITE_FAILED
    else:
        _log(row, "succeeded")
        return kind.outcome(plan)
    _log(row, "failed", reason)
    try:
        _record_unmerge_failed(row, dependencies, config, plan=plan, failed_reason=reason)
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"failed_row_not_recorded:{type(exc).__name__}")
    return _error(message)


def _replan_unmerge(
    row: PendingApprovalRow,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
    *,
    merged_id: str,
    merge_approval_id: str,
    kind: str,
    digest: str,
) -> tuple[GraphHandle, UnmergePlan] | dict[str, object]:
    """Step 2: re-locate the merge, re-read and re-plan; the plan, or the error outcome."""

    def _refused(refusal: _Refusal) -> dict[str, object]:
        return _refuse_unmerge(
            row, dependencies, config, kind=kind, merged_id=merged_id, refusal=refusal
        )

    try:
        graph = dependencies.open_single_tenant_graph(config)
        plan = plan_unmerge(graph, dependencies.audit_store(config), merged_id=merged_id)
    except GraphCleanupValidationError as exc:
        return _refused(_Refusal("revalidation_rejected", str(exc)))
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unreadable:{type(exc).__name__}")
        return _error(_AUDIT_UNREADABLE)
    except Exception as exc:  # noqa: BLE001 -- opening/reading the graph may fail in driver-specific ways; the message returned never carries them
        return _refused(
            _Refusal("graph_unavailable", _UNAVAILABLE, f"graph_unavailable:{type(exc).__name__}")
        )
    if (
        plan.state_digest != digest
        or plan.preview.merge_approval_id != merge_approval_id
        or plan.preview.kind != kind
    ):
        return _refused(_Refusal("state_digest_mismatch", _STALE))
    return graph, plan


def execute_unmerge(
    row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> dict[str, object]:
    """Execute one signed `unmerge` approval; return its outcome dict.

    Reverses an earlier Capability, Obligation or Role merge. Follows the same fail-closed order as
    the merges, and also re-locates the merge in the audit trail: it must still be the one the
    approval was created for.

    Args:
        row: The verified approval; its `normalized_args` carry the signed arguments and its
            actor identifies who approved.
        config: Service configuration used to open the graph and the stores.
        dependencies: Factories for the graph, audit store, access-role store and gateway.

    Returns:
        On success `{"merged_id", "survivor_id", "unmerged": True, "survivor_added_edges"}`, or for
        a Role `{"merged_id", "survivor_id", "unmerged": True,
        "survivor_obligations_added_since_merge"}`. On any expected failure
        `{"error": <short message without internal detail>}`; this function does not raise for
        them. A `kind` other than `capability`, `obligation` or `role` is a malformed approval.

    Side Effects:
        Writes an `applied` unmerge audit row, then one gateway group that restores the absorbed
        node and its edges. When the guard or the write fails, records a `failed` row with the
        same `approval_id` (best effort). A Role unmerge refused before the write (lost authority,
        drift, a conflict, a dependent merge, an unreachable graph) records a `failed`
        `role.unmerge` row too. Emits a structured log entry.
    """
    merged_id = _string_arg(row, "merged_id")
    merge_approval_id = _string_arg(row, "merge_approval_id")
    kind = _string_arg(row, "kind")
    digest = _string_arg(row, "state_digest")
    if (
        merged_id is None
        or merge_approval_id is None
        or digest is None
        or kind not in _UNMERGE_KINDS
    ):
        _log(row, "failed", "malformed_normalized_args")
        return _error(_BAD_APPROVAL)
    if not _actor_is_still_authorised(row, dependencies, config):
        return _refuse_unmerge(
            row,
            dependencies,
            config,
            kind=kind,
            merged_id=merged_id,
            refusal=_Refusal("actor_not_authorised", _NOT_AUTHORISED),
        )
    replanned = _replan_unmerge(
        row,
        config,
        dependencies,
        merged_id=merged_id,
        merge_approval_id=merge_approval_id,
        kind=kind,
        digest=digest,
    )
    if isinstance(replanned, dict):
        return replanned
    graph, plan = replanned
    return _write_unmerge_after_audit(row, dependencies, config, graph, plan=plan)


def verify_unmerge_effect(row: PendingApprovalRow, graph: GraphHandle) -> bool:
    """Report whether the approved unmerge is present in the graph.

    Args:
        row: The approval; needs a string `merged_id` and a `kind` of `capability`, `obligation`
            or `role` in `normalized_args` (any other kind is judged as a Capability).
        graph: Handle on the single-tenant graph, read only.

    Returns:
        For a Capability, True when it exists again and its `MergedCapability` marker is gone. For
        an Obligation, True when it exists again and its `MergedObligation` marker is gone. For a
        Role, True when it exists again as a Role and its `MergedRole` marker is gone. False
        otherwise, or when `merged_id` is missing.
    """
    merged_id = _string_arg(row, "merged_id")
    if merged_id is None:
        return False
    kind = _UNMERGE_KINDS.get(_string_arg(row, "kind") or "", _CAPABILITY_UNMERGE)
    return kind.verify(graph, merged_id)


def _execute_unmerge_registered(
    row: PendingApprovalRow, config: ServiceConfig
) -> dict[str, object]:
    return execute_unmerge(row, config, build_default_graph_cleanup_dependencies())


register_approval_executor(TOOL_UNMERGE, _execute_unmerge_registered)
register_effect_verifier(TOOL_UNMERGE, verify_unmerge_effect)
