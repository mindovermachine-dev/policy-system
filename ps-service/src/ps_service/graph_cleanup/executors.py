"""Executors run when a signed passkey approval is verified (issue #190).

`execute_capability_merge` and `execute_obligation_merge` are the post-signature steps of
`merge-capabilities` and `merge-obligations` (CHANGES.md Decision 4, audit-first and
fail-closed). Order:

1. re-check the approving actor still holds `ComplianceOfficer` (a store outage denies);
2. re-read the graph, re-validate, and compare the state digest bound into the signed args
   (drift means the previewed state is gone: nothing is written, no audit row);
3. record the `applied` audit row, then write -- if the audit row cannot be recorded, no
   graph edit is made (AC-BI-022);
4. one guarded statement (all-or-nothing, AC-BI-018); a guard miss or failure records a
   `failed` row with the same `approval_id` (best effort) and returns a generic error.

The function never raises for an expected failure; every error outcome is a short message
with no internal detail. The module registers its executor and effect verifier with
`passkey_signing.executors` at import (same idiom as `register_audit_action`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from ps_service.api.errors import AccessDeniedError, AuthorizationStoreUnavailableError
from ps_service.audit.errors import (
    AuditInvalidDetailsError,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditUnknownActionError,
)
from ps_service.authz.models import AccessRole
from ps_service.authz.service import require_role
from ps_service.graph_cleanup.audit_actions import (
    CAPABILITY_MERGE_ACTION,
    CAPABILITY_RELEASE_GOVERNANCE_ACTION,
    CAPABILITY_UNMERGE_ACTION,
    OBLIGATION_MERGE_ACTION,
    OBLIGATION_UNMERGE_ACTION,
    CapabilityMergeDetails,
    CapabilityReleaseGovernanceDetails,
    CapabilityUnmergeDetails,
    ObligationMergeDetails,
    ObligationUnmergeDetails,
)
from ps_service.graph_cleanup.dependencies import build_default_graph_cleanup_dependencies
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_capability_tombstone,
    read_capability_unmerged,
    read_governed_by_edge_present,
    read_obligation_marker,
    read_obligation_marker_present,
    read_obligation_merge_state,
    read_obligation_present,
    read_release_state,
)
from ps_service.graph_cleanup.graph_writer import (
    merge_capabilities,
    merge_obligations,
    release_governance,
    unmerge_capability,
    unmerge_obligation,
)
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.models import CapabilityUnmergePlan
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.graph_cleanup.unmerge import plan_unmerge
from ps_service.logging import emit_log_entry
from ps_service.passkey_signing.executors import (
    register_approval_executor,
    register_effect_verifier,
)

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.graph_cleanup.models import (
        CapabilityMergePlan,
        ObligationMergePlan,
        ReleasePlan,
    )
    from ps_service.graph_cleanup.unmerge import UnmergePlan
    from ps_service.passkey_signing.models import PendingApprovalRow

__all__ = [
    "TOOL_MERGE_CAPABILITIES",
    "TOOL_MERGE_OBLIGATIONS",
    "TOOL_RELEASE_GOVERNANCE",
    "TOOL_UNMERGE",
    "execute_capability_merge",
    "execute_obligation_merge",
    "execute_release_governance",
    "execute_unmerge",
    "verify_capability_merge_effect",
    "verify_obligation_merge_effect",
    "verify_release_governance_effect",
    "verify_unmerge_effect",
]

TOOL_MERGE_CAPABILITIES = "merge-capabilities"
TOOL_MERGE_OBLIGATIONS = "merge-obligations"
TOOL_RELEASE_GOVERNANCE = "release-capability-governance"
TOOL_UNMERGE = "unmerge"
_COMPONENT = "graph_cleanup"
_UNMERGE_KINDS = ("capability", "obligation")
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
        TOOL_RELEASE_GOVERNANCE: "execute_release_governance",
        TOOL_UNMERGE: "execute_unmerge",
    }.get(row.tool_name, "execute_capability_merge")
    emit_log_entry(component=_COMPONENT, action=action, outcome=outcome, extra=extra)


def _string_arg(row: PendingApprovalRow, name: str) -> str | None:
    value = row.normalized_args.get(name)
    return value if isinstance(value, str) else None


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


def _actor_is_still_authorised(
    row: PendingApprovalRow, dependencies: GraphCleanupDependencies, config: ServiceConfig
) -> bool:
    try:
        require_role(
            (row.actor_subject, row.actor_issuer),
            minimum=AccessRole.COMPLIANCE_OFFICER,
            store=dependencies.access_role_store(config),
        )
    except AccessDeniedError, AuthorizationStoreUnavailableError:
        return False
    return True


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
    """Steps 3-4: record `applied`, then write; on failure record `failed`, return the error."""
    try:
        _record(
            row,
            dependencies,
            config,
            outcome="applied",
            absorbed_id=absorbed_id,
            details=_details(row, survivor_id=survivor_id, absorbed_id=absorbed_id, plan=plan),
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_NOT_AUDITED)
    reason: Literal["graph_guard_missed", "graph_write_failed"]
    try:
        merge_capabilities(
            graph, survivor_id=survivor_id, absorbed_id=absorbed_id, expected=plan.expected
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


def _replan_capability_merge(
    row: PendingApprovalRow,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
    *,
    survivor_id: str,
    absorbed_id: str,
    digest: str,
) -> tuple[GraphHandle, CapabilityMergePlan] | dict[str, object]:
    """Step 2: re-read and re-validate; the plan, or the error outcome to return."""
    try:
        graph = dependencies.open_single_tenant_graph(config)
        state = read_capability_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
        plan = plan_capability_merge(state)
    except GraphCleanupValidationError as exc:
        _log(row, "failed", "revalidation_rejected")
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001 -- opening/reading the graph may fail in driver-specific ways; the message returned never carries them
        _log(row, "failed", f"graph_unavailable:{type(exc).__name__}")
        return _error(_UNAVAILABLE)
    governance = plan.preview.governance
    if (
        governance is not None
        and governance.acknowledgment_required
        and row.normalized_args.get("acknowledge_governance_change") is not True
    ):
        # Defence in depth (D5): the approval flow never creates such a row.
        _log(row, "failed", "acknowledgment_missing")
        return _error(_ACK_REQUIRED)
    if plan.state_digest != digest:
        _log(row, "failed", "state_digest_mismatch")
        return _error(_STALE)
    return graph, plan


def execute_capability_merge(
    row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> dict[str, object]:
    """Execute one signed `merge-capabilities` approval; return its outcome dict.

    Success: `{"survivor_id", "absorbed_id", "merged": True}`. Every failure is
    `{"error": <message without internal detail>}`. A case-2 merge (exactly one capability
    governed) is refused unless the signed arguments carry the acknowledgment.
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
    """Whether the approved merge is present in the graph (tombstone with `MERGED_INTO`)."""
    survivor_id = _string_arg(row, "survivor_id")
    absorbed_id = _string_arg(row, "absorbed_id")
    if survivor_id is None or absorbed_id is None:
        return False
    return read_capability_tombstone(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)


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
    """Steps 3-4 for an obligation merge: record `applied`, then write; `failed` on error."""
    try:
        _record_obligation(
            row,
            dependencies,
            config,
            outcome="applied",
            absorbed_id=absorbed_id,
            details=_obligation_details(
                row, survivor_id=survivor_id, absorbed_id=absorbed_id, plan=plan
            ),
        )
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_NOT_AUDITED)
    reason: Literal["graph_guard_missed", "graph_write_failed"]
    try:
        merge_obligations(
            graph, survivor_id=survivor_id, absorbed_id=absorbed_id, expected=plan.expected
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

    Same fail-closed order as `execute_capability_merge`. Success:
    `{"survivor_id", "absorbed_id", "merged": True}`. Every failure is
    `{"error": <message without internal detail>}`.
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
    """Whether the approved merge is present: the absorbed node is gone and its marker exists."""
    survivor_id = _string_arg(row, "survivor_id")
    absorbed_id = _string_arg(row, "absorbed_id")
    if survivor_id is None or absorbed_id is None:
        return False
    return not read_obligation_present(graph, obligation_id=absorbed_id) and read_obligation_marker(
        graph, survivor_id=survivor_id, absorbed_id=absorbed_id
    )


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
    """Steps 3-4 for a release: record `applied`, then delete the edge; `failed` on error."""
    try:
        _record_release(row, dependencies, config, outcome="applied", plan=plan)
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_RELEASE_NOT_AUDITED)
    reason: Literal["graph_guard_missed", "graph_write_failed"]
    try:
        release_governance(
            graph, capability_id=plan.preview.capability_id, policy_id=plan.preview.policy_id
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

    Same fail-closed order as the merges. Success:
    `{"capability_id", "policy_id", "released": True}`. Every failure is
    `{"error": <message without internal detail>}`.
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
    """Whether the approved release is present: the `GOVERNED_BY` edge is gone."""
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


register_approval_executor(TOOL_RELEASE_GOVERNANCE, _execute_release_registered)
register_effect_verifier(TOOL_RELEASE_GOVERNANCE, verify_release_governance_effect)


def _unmerge_details(
    row: PendingApprovalRow,
    *,
    plan: UnmergePlan,
    failed_reason: Literal["graph_guard_missed", "graph_write_failed"] | None = None,
) -> dict[str, object]:
    preview = plan.preview
    if isinstance(plan, CapabilityUnmergePlan):
        return CapabilityUnmergeDetails(
            survivor_id=plan.preview.survivor_id,
            absorbed_id=plan.preview.merged_id,
            approval_id=row.id,
            reverses_approval_id=plan.preview.merge_approval_id,
            policy_case=plan.inputs.policy_case,
            before=plan.before,
            after=plan.after,
            restored_edges=plan.restored_edges,
            survivor_added_edges=plan.preview.survivor_added_edges,
            reason_code=failed_reason,
        ).model_dump()
    return ObligationUnmergeDetails(
        survivor_id=preview.survivor_id,
        absorbed_id=preview.merged_id,
        role_id=plan.inputs.role_id,
        approval_id=row.id,
        reverses_approval_id=preview.merge_approval_id,
        before=plan.before,
        after=plan.after,
        restored_edges=plan.restored_edges,
        survivor_added_edges=plan.preview.survivor_added_edges,
        survivor_edges_possibly_from_merge=plan.preview.survivor_edges_possibly_from_merge,
        reason_code=failed_reason,
    ).model_dump()


def _record_unmerge(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    *,
    outcome: Literal["applied", "failed"],
    plan: UnmergePlan,
    failed_reason: Literal["graph_guard_missed", "graph_write_failed"] | None = None,
) -> None:
    is_capability = isinstance(plan, CapabilityUnmergePlan)
    _record(
        row,
        dependencies,
        config,
        outcome=outcome,
        absorbed_id=plan.preview.merged_id,
        details=_unmerge_details(row, plan=plan, failed_reason=failed_reason),
        action=CAPABILITY_UNMERGE_ACTION if is_capability else OBLIGATION_UNMERGE_ACTION,
        resource_type="capability" if is_capability else "obligation",
    )


def _run_unmerge_write(graph: GraphHandle, plan: UnmergePlan) -> None:
    """The one guarded writer statement for the plan's kind."""
    if isinstance(plan, CapabilityUnmergePlan):
        unmerge_capability(
            graph,
            absorbed_id=plan.preview.merged_id,
            survivor_id=plan.preview.survivor_id,
            write=plan.write,
        )
    else:
        unmerge_obligation(
            graph,
            absorbed_id=plan.preview.merged_id,
            survivor_id=plan.preview.survivor_id,
            write=plan.write,
        )


def _write_unmerge_after_audit(
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies,
    config: ServiceConfig,
    graph: GraphHandle,
    *,
    plan: UnmergePlan,
) -> dict[str, object]:
    """Steps 3-4 for an unmerge: record `applied`, then write; `failed` on error."""
    try:
        _record_unmerge(row, dependencies, config, outcome="applied", plan=plan)
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unavailable:{type(exc).__name__}")
        return _error(_UNMERGE_NOT_AUDITED)
    reason: Literal["graph_guard_missed", "graph_write_failed"]
    try:
        _run_unmerge_write(graph, plan)
    except GraphCleanupStaleStateError:
        reason, message = "graph_guard_missed", _STALE
    except GraphCleanupPersistenceError:
        reason, message = "graph_write_failed", _UNMERGE_WRITE_FAILED
    else:
        _log(row, "succeeded")
        return {
            "merged_id": plan.preview.merged_id,
            "survivor_id": plan.preview.survivor_id,
            "unmerged": True,
            "survivor_added_edges": [
                edge.model_dump(mode="json") for edge in plan.preview.survivor_added_edges
            ],
        }
    _log(row, "failed", reason)
    try:
        _record_unmerge(
            row, dependencies, config, outcome="failed", plan=plan, failed_reason=reason
        )
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
    try:
        graph = dependencies.open_single_tenant_graph(config)
        plan = plan_unmerge(graph, dependencies.audit_store(config), merged_id=merged_id)
    except GraphCleanupValidationError as exc:
        _log(row, "failed", "revalidation_rejected")
        return _error(str(exc))
    except _AUDIT_ERRORS as exc:
        _log(row, "failed", f"audit_unreadable:{type(exc).__name__}")
        return _error(_AUDIT_UNREADABLE)
    except Exception as exc:  # noqa: BLE001 -- opening/reading the graph may fail in driver-specific ways; the message returned never carries them
        _log(row, "failed", f"graph_unavailable:{type(exc).__name__}")
        return _error(_UNAVAILABLE)
    if (
        plan.state_digest != digest
        or plan.preview.merge_approval_id != merge_approval_id
        or plan.preview.kind != kind
    ):
        _log(row, "failed", "state_digest_mismatch")
        return _error(_STALE)
    return graph, plan


def execute_unmerge(
    row: PendingApprovalRow, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> dict[str, object]:
    """Execute one signed `unmerge` approval; return its outcome dict.

    Same fail-closed order as the merges, plus: the merge is re-located in the audit trail and
    must still be the one the approval was created for. Success:
    `{"merged_id", "survivor_id", "unmerged": True, "survivor_added_edges"}`. Every failure is
    `{"error": <message without internal detail>}`.
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
        _log(row, "failed", "actor_not_authorised")
        return _error(_NOT_AUTHORISED)
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
    """Whether the approved unmerge is present.

    A capability is active again with no redirect; an obligation exists again and its
    `MergedObligation` marker is gone.
    """
    merged_id = _string_arg(row, "merged_id")
    if merged_id is None:
        return False
    if _string_arg(row, "kind") == "obligation":
        return read_obligation_present(
            graph, obligation_id=merged_id
        ) and not read_obligation_marker_present(graph, obligation_id=merged_id)
    return read_capability_unmerged(graph, capability_id=merged_id)


def _execute_unmerge_registered(
    row: PendingApprovalRow, config: ServiceConfig
) -> dict[str, object]:
    return execute_unmerge(row, config, build_default_graph_cleanup_dependencies())


register_approval_executor(TOOL_UNMERGE, _execute_unmerge_registered)
register_effect_verifier(TOOL_UNMERGE, verify_unmerge_effect)
