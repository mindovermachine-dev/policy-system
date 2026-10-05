"""Orchestration for `ps_service.graph_cleanup` (issue #190).

Read-only candidate discovery (Capabilities by name and cached-embedding similarity,
duplicate Obligations within one Role), the `merge-capabilities` preview and pair-bound
passkey approval, and `check-cleanup-approval` with its lazy reconciler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

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
from ps_service.graph_cleanup.discovery import (
    DEFAULT_CAPABILITY_MIN_SIMILARITY,
    group_capabilities,
    group_duplicate_obligations,
)
from ps_service.graph_cleanup.errors import GraphCleanupAcknowledgmentRequiredError
from ps_service.graph_cleanup.executors import (
    TOOL_MERGE_CAPABILITIES,
    TOOL_MERGE_OBLIGATIONS,
    TOOL_RELEASE_GOVERNANCE,
    TOOL_UNMERGE,
)
from ps_service.graph_cleanup.graph_reader import (
    read_active_capabilities,
    read_capability_merge_state,
    read_obligation_merge_state,
    read_obligations_by_role,
    read_release_state,
)
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.models import (
    CapabilityMergePlan,
    CapabilityMergePreview,
    CapabilityUnmergePreview,
    FindCapabilityCandidatesResult,
    FindDuplicateObligationsResult,
    ObligationMergePlan,
    ObligationMergePreview,
    ObligationUnmergePreview,
    ReleasePlan,
    ReleasePreview,
)
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.graph_cleanup.unmerge import UnmergePlan, plan_unmerge
from ps_service.logging import emit_log_entry
from ps_service.passkey_signing.executors import resolve_effect_verifier

if TYPE_CHECKING:
    from typing import Literal

    from ps_service.audit.store import AuditStore
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.logging import LogEmitter
    from ps_service.passkey_signing.models import PendingApprovalRow
    from ps_service.passkey_signing.store import PendingApprovalStore

__all__ = [
    "RECONCILE_GRACE",
    "CapabilityMergeApproval",
    "CleanupApprovalStatus",
    "ObligationMergeApproval",
    "ReleaseGovernanceApproval",
    "UnmergeApproval",
    "check_cleanup_approval",
    "create_capability_merge_approval",
    "create_obligation_merge_approval",
    "create_release_governance_approval",
    "create_unmerge_approval",
    "find_capability_merge_candidates",
    "find_duplicate_obligations",
    "preview_capability_merge",
    "preview_obligation_merge",
    "preview_release_governance",
    "preview_unmerge",
]

_COMPONENT = "graph_cleanup"
_CASES_WITH_GOVERNANCE = (2, 3)


def find_capability_merge_candidates(
    graph: GraphHandle,
    *,
    min_similarity: float = DEFAULT_CAPABILITY_MIN_SIMILARITY,
    emitter: LogEmitter | None = None,
) -> FindCapabilityCandidatesResult:
    """Return groups of active Capabilities that look like the same duty.

    Linked by equal normalised names or by cached-embedding cosine similarity at or
    above `min_similarity`; no embedding is ever fetched or computed.

    Read-only: nothing is written and no approval is created.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    capabilities = read_active_capabilities(graph)
    groups = group_capabilities(capabilities, min_similarity=min_similarity)
    emit_log_entry(
        component=_COMPONENT,
        action="find_capability_merge_candidates",
        outcome="succeeded",
        extra={
            "capability_count": len(capabilities),
            "group_count": len(groups),
            "min_similarity": min_similarity,
        },
        emitter=emitter,
    )
    return FindCapabilityCandidatesResult(groups=groups)


def find_duplicate_obligations(
    graph: GraphHandle,
    *,
    role_id: str | None = None,
    emitter: LogEmitter | None = None,
) -> FindDuplicateObligationsResult:
    """Return groups of Obligations under ONE Role with identical or near-identical text.

    `role_id` limits the sweep to one Role. Each member lists its Requirements'
    `source_ref`s. Read-only: nothing is written and no approval is created.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    obligations = read_obligations_by_role(graph, role_id=role_id)
    groups = group_duplicate_obligations(obligations)
    emit_log_entry(
        component=_COMPONENT,
        action="find_duplicate_obligations",
        outcome="succeeded",
        extra={
            "obligation_count": len(obligations),
            "group_count": len(groups),
            "role_filtered": role_id is not None,
        },
        emitter=emitter,
    )
    return FindDuplicateObligationsResult(groups=groups)


def _plan(graph: GraphHandle, *, survivor_id: str, absorbed_id: str) -> CapabilityMergePlan:
    state = read_capability_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    return plan_capability_merge(state)


def preview_capability_merge(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str
) -> CapabilityMergePreview:
    """Preview merging `absorbed_id` into `survivor_id` with no writes (AC-BI-005).

    Raises:
        GraphCleanupValidationError: self-merge, a nonexistent or `merged` side, or two
            capabilities governed by different policies (AC-BI-011).
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _plan(graph, survivor_id=survivor_id, absorbed_id=absorbed_id).preview


@dataclass(frozen=True, slots=True)
class CapabilityMergeApproval:
    """A previewed merge and the pending passkey approval bound to it."""

    preview: CapabilityMergePreview
    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601


def create_capability_merge_approval(
    graph: GraphHandle,
    *,
    survivor_id: str,
    absorbed_id: str,
    acknowledge_governance_change: bool,
    actor: tuple[str, str],
    base_url: str,
    store: PendingApprovalStore,
    emitter: LogEmitter | None = None,
) -> CapabilityMergeApproval:
    """Preview the merge and create a passkey approval bound to the exact pair and state.

    The signed `normalized_args` carry both ids, the acknowledgment flag and the sha256
    `state_digest` of the previewed state, so the signature covers the pair and the state
    the officer actually saw. Writes nothing to the graph; a rejected merge creates no row.

    A case-2 pair (exactly one capability governed) needs the officer's explicit
    acknowledgment of the governance change in addition to the passkey: without
    `acknowledge_governance_change` no approval is created (D5). A case-3 pair (both governed
    by the SAME policy) needs none (AC-BI-012); different policies are rejected up front.

    Raises:
        GraphCleanupValidationError: see `preview_capability_merge`; no approval is created.
        GraphCleanupAcknowledgmentRequiredError: case 2 without the acknowledgment; carries the
            preview, no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    plan = _plan(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    governance = plan.preview.governance
    if (
        governance is not None
        and governance.acknowledgment_required
        and not acknowledge_governance_change
    ):
        emit_log_entry(
            component=_COMPONENT,
            action="create_capability_merge_approval",
            outcome="acknowledgment_required",
            extra={"policy_case": plan.preview.policy_case, "policy_id": governance.policy.id},
            emitter=emitter,
        )
        raise GraphCleanupAcknowledgmentRequiredError(
            plan.preview,
            f"{governance.acknowledgment_text} Repeat the call with "
            "acknowledge_governance_change=true to receive the approval link.",
        )
    row, code = store.create_pending_approval(
        tool_name=TOOL_MERGE_CAPABILITIES,
        normalized_args={
            "survivor_id": survivor_id,
            "absorbed_id": absorbed_id,
            "acknowledge_governance_change": acknowledge_governance_change,
            "state_digest": plan.state_digest,
        },
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary=plan.preview.model_dump(mode="json"),
    )
    emit_log_entry(
        component=_COMPONENT,
        action="create_capability_merge_approval",
        outcome="succeeded",
        extra={"approval_id": row.id, "policy_case": plan.preview.policy_case},
        emitter=emitter,
    )
    return CapabilityMergeApproval(
        preview=plan.preview,
        pending_approval_id=row.id,
        approval_url=f"{base_url}/approvals/{row.id}#{code}",
        expires_at=row.expires_at.isoformat(),
    )


def _plan_obligations(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str
) -> ObligationMergePlan:
    state = read_obligation_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    return plan_obligation_merge(state)


def preview_obligation_merge(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str
) -> ObligationMergePreview:
    """Preview merging obligation `absorbed_id` into `survivor_id` with no writes (AC-BI-005).

    Raises:
        GraphCleanupValidationError: self-merge, a nonexistent side, or two Obligations under
            different Roles (AC-BI-015/016).
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _plan_obligations(graph, survivor_id=survivor_id, absorbed_id=absorbed_id).preview


@dataclass(frozen=True, slots=True)
class ObligationMergeApproval:
    """A previewed obligation merge and the pending passkey approval bound to it."""

    preview: ObligationMergePreview
    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601


def create_obligation_merge_approval(
    graph: GraphHandle,
    *,
    survivor_id: str,
    absorbed_id: str,
    actor: tuple[str, str],
    base_url: str,
    store: PendingApprovalStore,
    emitter: LogEmitter | None = None,
) -> ObligationMergeApproval:
    """Preview the obligation merge and create a passkey approval bound to the pair and state.

    The signed `normalized_args` carry both ids and the sha256 `state_digest` of the previewed
    state. Writes nothing to the graph; a rejected merge creates no row.

    Raises:
        GraphCleanupValidationError: see `preview_obligation_merge`; no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    plan = _plan_obligations(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    row, code = store.create_pending_approval(
        tool_name=TOOL_MERGE_OBLIGATIONS,
        normalized_args={
            "survivor_id": survivor_id,
            "absorbed_id": absorbed_id,
            "state_digest": plan.state_digest,
        },
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary=plan.preview.model_dump(mode="json"),
    )
    emit_log_entry(
        component=_COMPONENT,
        action="create_obligation_merge_approval",
        outcome="succeeded",
        extra={"approval_id": row.id, "role_id": plan.preview.role_id},
        emitter=emitter,
    )
    return ObligationMergeApproval(
        preview=plan.preview,
        pending_approval_id=row.id,
        approval_url=f"{base_url}/approvals/{row.id}#{code}",
        expires_at=row.expires_at.isoformat(),
    )


def _plan_release(graph: GraphHandle, *, capability_id: str) -> ReleasePlan:
    return plan_release_governance(read_release_state(graph, capability_id=capability_id))


def preview_release_governance(graph: GraphHandle, *, capability_id: str) -> ReleasePreview:
    """Preview releasing `capability_id` from its draft governing policy, with no writes.

    Raises:
        GraphCleanupValidationError: the capability does not exist, is a tombstone, is not
            governed, or its governing policy is not a draft (AC-BI-014, D12).
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _plan_release(graph, capability_id=capability_id).preview


@dataclass(frozen=True, slots=True)
class ReleaseGovernanceApproval:
    """A previewed governance release and the pending passkey approval bound to it."""

    preview: ReleasePreview
    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601


def create_release_governance_approval(
    graph: GraphHandle,
    *,
    capability_id: str,
    actor: tuple[str, str],
    base_url: str,
    store: PendingApprovalStore,
    emitter: LogEmitter | None = None,
) -> ReleaseGovernanceApproval:
    """Preview the release and create a passkey approval bound to the capability and policy.

    The signed `normalized_args` carry the capability id, the governing policy id and the sha256
    `state_digest` of the previewed state. Writes nothing to the graph; a rejected release
    (any non-draft policy included) creates no row.

    Raises:
        GraphCleanupValidationError: see `preview_release_governance`; no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    plan = _plan_release(graph, capability_id=capability_id)
    row, code = store.create_pending_approval(
        tool_name=TOOL_RELEASE_GOVERNANCE,
        normalized_args={
            "capability_id": capability_id,
            "policy_id": plan.preview.policy_id,
            "state_digest": plan.state_digest,
        },
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary=plan.preview.model_dump(mode="json"),
    )
    emit_log_entry(
        component=_COMPONENT,
        action="create_release_governance_approval",
        outcome="succeeded",
        extra={"approval_id": row.id, "policy_id": plan.preview.policy_id},
        emitter=emitter,
    )
    return ReleaseGovernanceApproval(
        preview=plan.preview,
        pending_approval_id=row.id,
        approval_url=f"{base_url}/approvals/{row.id}#{code}",
        expires_at=row.expires_at.isoformat(),
    )


def preview_unmerge(
    graph: GraphHandle, audit_store: AuditStore, *, merged_id: str
) -> CapabilityUnmergePreview | ObligationUnmergePreview:
    """Preview reversing the newest effective merge of `merged_id`, with no writes (AC-BI-019).

    Raises:
        GraphCleanupValidationError: no merge to reverse, or a conflict (AC-BI-020) explained.
        GraphCleanupPersistenceError: the graph database could not be read.
        AuditPostgresUnavailableError: the audit trail could not be read.
    """
    return plan_unmerge(graph, audit_store, merged_id=merged_id).preview


@dataclass(frozen=True, slots=True)
class UnmergeApproval:
    """A previewed unmerge and the pending passkey approval bound to it."""

    preview: CapabilityUnmergePreview | ObligationUnmergePreview
    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601


def create_unmerge_approval(
    graph: GraphHandle,
    audit_store: AuditStore,
    *,
    merged_id: str,
    actor: tuple[str, str],
    base_url: str,
    store: PendingApprovalStore,
    emitter: LogEmitter | None = None,
) -> UnmergeApproval:
    """Preview the unmerge and create a passkey approval bound to the merge and the state.

    The signed `normalized_args` carry the merged id, the approval id of the merge being
    reversed, the kind and the sha256 `state_digest` of the previewed state. Writes nothing to
    the graph; a rejected or conflicting unmerge creates no row.

    Raises:
        GraphCleanupValidationError: see `preview_unmerge`; no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.
        AuditPostgresUnavailableError: the audit trail could not be read.
    """
    plan: UnmergePlan = plan_unmerge(graph, audit_store, merged_id=merged_id)
    row, code = store.create_pending_approval(
        tool_name=TOOL_UNMERGE,
        normalized_args={
            "merged_id": merged_id,
            "merge_approval_id": plan.preview.merge_approval_id,
            "kind": plan.preview.kind,
            "state_digest": plan.state_digest,
        },
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary=plan.preview.model_dump(mode="json"),
    )
    emit_log_entry(
        component=_COMPONENT,
        action="create_unmerge_approval",
        outcome="succeeded",
        extra={"approval_id": row.id, "kind": plan.preview.kind},
        emitter=emitter,
    )
    return UnmergeApproval(
        preview=plan.preview,
        pending_approval_id=row.id,
        approval_url=f"{base_url}/approvals/{row.id}#{code}",
        expires_at=row.expires_at.isoformat(),
    )


# A signed row whose outcome was never recorded is only judged once this long past its
# expiry: long enough that the executor which signed it cannot still be running. The
# FalkorDB client sets no socket timeout; the server-side query timeout is far shorter.
RECONCILE_GRACE = timedelta(seconds=300)

_CLEANUP_TOOL_NAMES = frozenset(
    {TOOL_MERGE_CAPABILITIES, TOOL_MERGE_OBLIGATIONS, TOOL_RELEASE_GOVERNANCE, TOOL_UNMERGE}
)
_INTERRUPTED_MESSAGE = (
    "the approved action did not take effect; "
    "if you still intend to proceed, ask for a new approval"
)


@dataclass(frozen=True, slots=True)
class CleanupApprovalStatus:
    """The caller's own cleanup approval: live status plus the stored outcome, if any."""

    pending_approval_id: str
    tool_name: str
    status: Literal["pending", "expired", "signed"]
    outcome: dict[str, object] | None


def _live_status(row: PendingApprovalRow) -> Literal["pending", "expired", "signed"]:
    if row.status == "signed":
        return "signed"
    return "expired" if datetime.now(UTC) > row.expires_at else "pending"


def _interrupted_audit_row(row: PendingApprovalRow) -> tuple[str, str, str, dict[str, object]]:
    """Action, resource type, resource id and details of the `failed` row for an orphan."""
    if row.tool_name == TOOL_RELEASE_GOVERNANCE:
        capability_id = str(row.normalized_args.get("capability_id", ""))
        release_details = CapabilityReleaseGovernanceDetails(
            capability_id=capability_id,
            policy_id=str(row.normalized_args.get("policy_id", "")),
            policy_status=str(row.display_summary.get("policy_status", "draft")),
            approval_id=row.id,
            reason_code="interrupted_no_effect",
        ).model_dump()
        return CAPABILITY_RELEASE_GOVERNANCE_ACTION, "capability", capability_id, release_details
    if row.tool_name == TOOL_UNMERGE:
        merged_id = str(row.normalized_args.get("merged_id", ""))
        if row.normalized_args.get("kind") == "obligation":
            obligation_unmerge = ObligationUnmergeDetails(
                survivor_id=str(row.display_summary.get("survivor_id", "")),
                absorbed_id=merged_id,
                role_id=str(row.display_summary.get("role_id", "")),
                approval_id=row.id,
                reverses_approval_id=str(row.normalized_args.get("merge_approval_id", "")),
                reason_code="interrupted_no_effect",
            ).model_dump()
            return OBLIGATION_UNMERGE_ACTION, "obligation", merged_id, obligation_unmerge
        unmerge_details = CapabilityUnmergeDetails(
            survivor_id=str(row.display_summary.get("survivor_id", "")),
            absorbed_id=merged_id,
            approval_id=row.id,
            reverses_approval_id=str(row.normalized_args.get("merge_approval_id", "")),
            reason_code="interrupted_no_effect",
        ).model_dump()
        return CAPABILITY_UNMERGE_ACTION, "capability", merged_id, unmerge_details
    survivor_id = str(row.normalized_args.get("survivor_id", ""))
    absorbed_id = str(row.normalized_args.get("absorbed_id", ""))
    case = row.display_summary.get("policy_case")
    if row.tool_name == TOOL_MERGE_OBLIGATIONS:
        obligation_details = ObligationMergeDetails(
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            role_id=str(row.display_summary.get("role_id", "")),
            approval_id=row.id,
            reason_code="interrupted_no_effect",
        ).model_dump()
        return OBLIGATION_MERGE_ACTION, "obligation", absorbed_id, obligation_details
    details = CapabilityMergeDetails(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        policy_case=case if case in _CASES_WITH_GOVERNANCE else 1,
        acknowledged=row.normalized_args.get("acknowledge_governance_change") is True,
        approval_id=row.id,
        reason_code="interrupted_no_effect",
    ).model_dump()
    return CAPABILITY_MERGE_ACTION, "capability", absorbed_id, details


def _reconcile_orphan(
    row: PendingApprovalRow,
    *,
    store: PendingApprovalStore,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
) -> dict[str, object] | None:
    """Settle a signed row that has no outcome, past the grace window (CHANGES.md A9).

    Effect present -> outcome `{"reconciled": "applied"}`. Effect absent -> a `failed`
    audit row (`interrupted_no_effect`, same approval id) first, then a failed outcome.
    Anything that raises leaves the row untouched so a later check retries.
    """
    verifier = resolve_effect_verifier(row.tool_name)
    if verifier is None:
        return None
    graph = dependencies.open_single_tenant_graph(config)
    if verifier(row, graph):
        outcome: dict[str, object] = {"reconciled": "applied"}
    else:
        action, resource_type, resource_id, details = _interrupted_audit_row(row)
        dependencies.audit_store(config).record_standalone(
            actor_subject=row.actor_subject,
            actor_issuer=row.actor_issuer,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome="failed",
            details=details,
        )
        outcome = {"error": _INTERRUPTED_MESSAGE}
    store.set_outcome(row.id, outcome)
    emit_log_entry(
        component=_COMPONENT,
        action="reconcile_cleanup_approval",
        outcome="succeeded",
        extra={"approval_id": row.id, "result": "applied" if "reconciled" in outcome else "failed"},
    )
    return outcome


def check_cleanup_approval(
    *,
    pending_approval_id: str,
    actor: tuple[str, str] | None,
    store: PendingApprovalStore,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
) -> CleanupApprovalStatus | None:
    """Return the caller's own cleanup approval, reconciling an orphaned signed row (A9).

    `None` for every case that must look like "not found": no resolvable actor, an unknown
    id, another actor's approval, or an approval that is not a cleanup approval. A signed
    row with no outcome that is past `expires_at + RECONCILE_GRACE` is verified against the
    graph first; see `_reconcile_orphan`.

    Raises:
        GraphCleanupPersistenceError: reconciliation could not read the graph.
        AuditPostgresUnavailableError / AuditPersistenceError: reconciliation could not
            record its `failed` row (the approval is left unreconciled).
    """
    if actor is None:
        return None
    row = store.get_by_id(pending_approval_id)
    if row is None or (row.actor_subject, row.actor_issuer) != actor:
        return None
    if row.tool_name not in _CLEANUP_TOOL_NAMES:
        return None
    outcome = row.outcome
    if (
        row.status == "signed"
        and outcome is None
        and datetime.now(UTC) > row.expires_at + RECONCILE_GRACE
    ):
        outcome = _reconcile_orphan(row, store=store, config=config, dependencies=dependencies)
    return CleanupApprovalStatus(
        pending_approval_id=row.id,
        tool_name=row.tool_name,
        status=_live_status(row),
        outcome=outcome,
    )
