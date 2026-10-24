"""Orchestration for `ps_service.graph_cleanup`.

Read-only candidate discovery (Capabilities by name and cached-embedding similarity,
duplicate Obligations within one Role), the read-only list of automatic Capability resolutions,
the previews of every cleanup action (Capability merge, Obligation merge, Role merge, governance
release, unmerge, split of a resolved Capability), the passkey approvals bound to a
previewed state, and `check-cleanup-approval` with its lazy reconciler. Nothing here writes
to the graph; the writes run in `ps_service.graph_cleanup.executors` after a signed approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.similarity import cosine_similarity
from ps_service.graph_cleanup.audit_actions import (
    CAPABILITY_MERGE_ACTION,
    CAPABILITY_RELEASE_GOVERNANCE_ACTION,
    CAPABILITY_SPLIT_ACTION,
    CAPABILITY_UNMERGE_ACTION,
    OBLIGATION_MERGE_ACTION,
    OBLIGATION_UNMERGE_ACTION,
    ROLE_MERGE_ACTION,
    ROLE_UNMERGE_ACTION,
    CapabilityMergeDetails,
    CapabilityReleaseGovernanceDetails,
    CapabilitySplitDetails,
    CapabilityUnmergeDetails,
    CleanupFailedReason,
    ObligationMergeDetails,
    ObligationUnmergeDetails,
    RoleMergeDetails,
    RoleUnmergeDetails,
)
from ps_service.graph_cleanup.discovery import (
    DEFAULT_CAPABILITY_MIN_SIMILARITY,
    group_capabilities,
    group_duplicate_obligations,
)
from ps_service.graph_cleanup.errors import (
    GraphCleanupAcknowledgmentRequiredError,
    GraphCleanupPersistenceError,
    GraphCleanupSplitAcknowledgmentRequiredError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.executors import (
    CLEANUP_TOOL_NAMES,
    TOOL_MERGE_CAPABILITIES,
    TOOL_MERGE_OBLIGATIONS,
    TOOL_MERGE_ROLES,
    TOOL_RELEASE_GOVERNANCE,
    TOOL_SPLIT_CAPABILITY,
    TOOL_UNMERGE,
)
from ps_service.graph_cleanup.graph_reader import (
    read_active_capabilities,
    read_capability_merge_state,
    read_capability_resolutions,
    read_obligation_merge_state,
    read_obligations_by_role,
    read_pending_review,
    read_release_state,
    read_resolution_terminals,
    read_role_merge_state,
)
from ps_service.graph_cleanup.graph_submission import graph_write_target, require_caught_up
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.models import (
    BaselineCapability,
    CapabilityMergePlan,
    CapabilityMergePreview,
    CapabilityResolution,
    CapabilityResolutionEntry,
    CapabilitySplitPlan,
    CapabilitySplitPreview,
    CapabilityUnmergePreview,
    FindCapabilityCandidatesResult,
    FindDuplicateObligationsResult,
    ListCapabilityResolutionsResult,
    ObligationMergePlan,
    ObligationMergePreview,
    ObligationUnmergePreview,
    ReleasePlan,
    ReleasePreview,
    ResolutionTerminal,
    RoleMergePlan,
    RoleMergePreview,
    RoleUnmergePreview,
)
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.graph_cleanup.role_planner import plan_role_merge, validate_role_review
from ps_service.graph_cleanup.split_planner import (
    HeldEdges,
    plan_capability_split,
    preview_policy_id,
)
from ps_service.graph_cleanup.split_reader import (
    read_capability_split_state,
    read_held_by_baselines,
    read_held_edges,
)
from ps_service.graph_cleanup.unmerge import UnmergePlan, plan_unmerge
from ps_service.logging import emit_log_entry
from ps_service.passkey_signing.executors import resolve_effect_verifier

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
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
    "CapabilitySplitApproval",
    "CleanupApprovalStatus",
    "ObligationMergeApproval",
    "ReleaseGovernanceApproval",
    "RoleMergeApproval",
    "UnmergeApproval",
    "check_cleanup_approval",
    "create_capability_merge_approval",
    "create_capability_split_approval",
    "create_obligation_merge_approval",
    "create_release_governance_approval",
    "create_role_merge_approval",
    "create_unmerge_approval",
    "failed_audit_row",
    "find_capability_merge_candidates",
    "find_duplicate_obligations",
    "graph_has_caught_up",
    "interrupted_audit_row",
    "list_capability_resolutions",
    "live_status",
    "preview_capability_merge",
    "preview_capability_split",
    "preview_obligation_merge",
    "preview_release_governance",
    "preview_role_merge",
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

    Capabilities are linked by equal normalised names or by cached-embedding cosine similarity
    at or above `min_similarity`; no embedding is ever fetched or computed.

    Args:
        graph: Handle on the single-tenant graph, read only.
        min_similarity: Cosine similarity cut-off in [0, 1] above which two cached embeddings
            link their Capabilities.
        emitter: Optional log emitter; None uses the default.

    Returns:
        The candidate groups. An empty `groups` means no active Capabilities look alike.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.

    Side Effects:
        Emits one structured log entry. Writes nothing and creates no approval.
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


def _source_node(
    holding: Sequence[tuple[str, BaselineCapability, tuple[str, ...]]],
) -> BaselineCapability | None:
    """The source baseline's node: the first holder with an embedding, else the first holder."""
    with_embedding = [node for _name, node, _ids in holding if node.embedding is not None]
    if with_embedding:
        return with_embedding[0]
    return holding[0][1] if holding else None


def _similarity(source: BaselineCapability | None, terminal: ResolutionTerminal) -> float | None:
    """Cosine of X's source-baseline embedding and A's cached one; None when either is missing.

    A pair of different lengths or a zero vector is unscorable and has no similarity, rather than
    failing the whole list.
    """
    if source is None or source.embedding is None or terminal.embedding is None:
        return None
    try:
        return cosine_similarity(source.embedding, terminal.embedding)
    except CompanyMergeValidationError:
        return None


def _requires_held(
    graph: GraphHandle,
    baselines: Sequence[tuple[str, GraphHandle]],
    resolutions: Sequence[CapabilityResolution],
) -> dict[str, HeldEdges]:
    """Each resolution's `REQUIRES` sources held on X and on A, from every baseline."""
    return read_held_edges(
        graph, baselines, "REQUIRES", [(r.x_id, r.a_aliases) for r in resolutions]
    )


def list_capability_resolutions(
    graph: GraphHandle,
    baselines: Sequence[tuple[str, GraphHandle]],
    *,
    emitter: LogEmitter | None = None,
) -> ListCapabilityResolutionsResult:
    """List every automatic Capability resolution with what a Compliance Officer needs to judge it.

    Each `ResolvedCapability` X is reported with its terminal Capability A (the end of its marker
    chain), A's governing Policy, X's name from a baseline that holds it, the cosine similarity of
    their cached embeddings, the instruments whose baseline holds X, and the number of existing
    Obligations a baseline links to X. Nothing is written and no embedding is fetched or computed.

    Args:
        graph: Handle on the single-tenant graph, read only.
        baselines: `(graph name, read handle)` of every `{short}_baseline` graph, read only.
        emitter: Optional log emitter; None uses the default.

    Returns:
        One entry per `ResolvedCapability`, sorted by X's id; empty when there is none.

    Raises:
        GraphCleanupValidationError: a marker chain is corrupt (a cycle, an id held by both
            marker labels, a marker without a target, a terminal that is not a Capability, or an
            Obligation marker cycle); the whole list is refused rather than an entry dropped.
        GraphCleanupPersistenceError: a graph could not be read.

    Side Effects:
        Emits one structured log entry. Writes nothing and creates no approval.
    """
    resolutions = read_capability_resolutions(graph)
    terminals = read_resolution_terminals(graph, {r.a_id for r in resolutions})
    for resolution in resolutions:
        if resolution.a_id not in terminals:
            message = (
                f"capability {resolution.x_id!r} resolves to {resolution.a_id!r}, "
                "which is not an existing Capability"
            )
            raise GraphCleanupValidationError(message)
    held = read_held_by_baselines(baselines, {r.x_id for r in resolutions}) if resolutions else {}
    requires = _requires_held(graph, baselines, resolutions) if resolutions else {}
    entries: list[CapabilityResolutionEntry] = []
    for resolution in resolutions:
        holding = held.get(resolution.x_id, [])
        source = _source_node(holding)
        terminal = terminals[resolution.a_id]
        entries.append(
            CapabilityResolutionEntry(
                x_id=resolution.x_id,
                x_name=source.name if source is not None else None,
                a_id=terminal.id,
                a_name=terminal.name,
                marker_resolves_to=resolution.marker_resolves_to,
                governing_policy=terminal.governing_policy,
                similarity=_similarity(source, terminal),
                instruments=tuple(sorted({i for _n, _node, ids in holding for i in ids})),
                obligation_count=len(requires[resolution.x_id].held_x),
            )
        )
    emit_log_entry(
        component=_COMPONENT,
        action="list_capability_resolutions",
        outcome="succeeded",
        extra={"resolution_count": len(entries), "baseline_count": len(baselines)},
        emitter=emitter,
    )
    return ListCapabilityResolutionsResult(resolutions=tuple(entries))


def find_duplicate_obligations(
    graph: GraphHandle,
    *,
    role_id: str | None = None,
    emitter: LogEmitter | None = None,
) -> FindDuplicateObligationsResult:
    """Return groups of Obligations under ONE Role with identical or near-identical text.

    Obligations under different Roles are never grouped. Each member lists its Requirements'
    `source_ref`s.

    Args:
        graph: Handle on the single-tenant graph, read only.
        role_id: Limits the sweep to this Role; None sweeps every Role.
        emitter: Optional log emitter; None uses the default.

    Returns:
        The duplicate groups. An empty `groups` means no duplicates were found.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.

    Side Effects:
        Emits one structured log entry. Writes nothing and creates no approval.
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
    """Preview merging Capability `absorbed_id` into `survivor_id` with no writes.

    Args:
        graph: Handle on the single-tenant graph, read only.
        survivor_id: Id of the Capability that remains; must differ from `absorbed_id`.
        absorbed_id: Id of the Capability that is deleted and replaced by a marker.

    Returns:
        The merge preview: what moves, the policy case and, when a policy is involved, the
        governance effect and acknowledgment text.

    Raises:
        GraphCleanupValidationError: self-merge, a nonexistent or non-active side, or two
            Capabilities governed by different policies.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _plan(graph, survivor_id=survivor_id, absorbed_id=absorbed_id).preview


@dataclass(frozen=True, slots=True)
class CapabilityMergeApproval:
    """A previewed Capability merge and the pending passkey approval bound to it.

    Attributes:
        preview: The merge preview the officer saw.
        pending_approval_id: Id of the pending approval row.
        approval_url: Link where the officer signs with a passkey; includes the one-time code.
        expires_at: ISO 8601 time after which the approval can no longer be signed.
    """

    preview: CapabilityMergePreview
    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601


def create_capability_merge_approval(  # noqa: PLR0913 -- the pair, the acknowledgment, the optional review, the actor and the approval collaborators; no natural grouping
    graph: GraphHandle,
    *,
    survivor_id: str,
    absorbed_id: str,
    acknowledge_governance_change: bool,
    actor: tuple[str, str],
    base_url: str,
    store: PendingApprovalStore,
    review_id: str | None = None,
    emitter: LogEmitter | None = None,
) -> CapabilityMergeApproval:
    """Preview the merge and create a passkey approval bound to the exact pair and state.

    The signed `normalized_args` carry both ids, the acknowledgment flag and the sha256
    `state_digest` of the previewed state, so the signature covers the pair and the state
    the officer actually saw. A `review_id` is signed with them and makes the executor delete
    that near-miss `PendingReview` in the merge group.

    Business Rules:
        A case-2 pair (exactly one Capability governed) needs the officer's explicit
        acknowledgment of the governance change in addition to the passkey: without
        `acknowledge_governance_change` no approval is created. A case-3 pair (both governed by
        the SAME policy) needs none. Pairs governed by different policies are rejected.

    Args:
        graph: Handle on the single-tenant graph, read only.
        survivor_id: Id of the Capability that remains; must differ from `absorbed_id`.
        absorbed_id: Id of the Capability that is deleted and replaced by a marker.
        acknowledge_governance_change: True when the officer accepts the governance change a
            case-2 merge makes.
        actor: `(subject, issuer)` of the requesting officer; the approval is bound to them.
        base_url: Origin used to build the approval link, without a trailing slash.
        store: Pending-approval store that holds the approval row.
        review_id: Id of the near-miss `PendingReview` this merge resolves; None for a plain
            cleanup merge.
        emitter: Optional log emitter; None uses the default.

    Returns:
        The preview with the approval id, signing URL and expiry.

    Raises:
        GraphCleanupValidationError: self-merge, a nonexistent or non-active side, a side with
            more than one governing policy, or both sides governed by different policies; no
            approval is created.
        GraphCleanupAcknowledgmentRequiredError: case 2 without the acknowledgment; carries the
            preview, no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.

    Side Effects:
        Creates one pending approval row and emits a structured log entry. Writes nothing to the
        graph; a rejected merge creates no row.
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
            **({"review_id": review_id} if review_id is not None else {}),
        },
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary=plan.preview.model_dump(mode="json"),
    )
    emit_log_entry(
        component=_COMPONENT,
        action="create_capability_merge_approval",
        outcome="succeeded",
        extra={
            "approval_id": row.id,
            "policy_case": plan.preview.policy_case,
            "review_id": review_id,
        },
        emitter=emitter,
    )
    return CapabilityMergeApproval(
        preview=plan.preview,
        pending_approval_id=row.id,
        approval_url=f"{base_url}/approvals/{row.id}#{code}",
        expires_at=row.expires_at.isoformat(),
    )


def _plan_split(
    graph: GraphHandle, baselines: Sequence[tuple[str, GraphHandle]], x_id: str
) -> CapabilitySplitPlan:
    return plan_capability_split(read_capability_split_state(graph, baselines, x_id))


def preview_capability_split(
    graph: GraphHandle, baselines: Sequence[tuple[str, GraphHandle]], *, x_id: str
) -> CapabilitySplitPreview:
    """Preview splitting the automatically resolved Capability `x_id` out of its terminal.

    Args:
        graph: Handle on the single-tenant graph, read only.
        baselines: `(graph name, read handle)` of every `{short}_baseline` graph, read only.
        x_id: Id of the `ResolvedCapability` to split.

    Returns:
        The split preview: per edge type the sources whose edge moves, is shared, is added or
        stays, A's governing Policy and the state digest.

    Raises:
        GraphCleanupValidationError: no `ResolvedCapability` has the id, it already exists as a
            Capability or as a `MergedCapability`, a marker chain is corrupt, the terminal is not
            a Capability, or no baseline holds the id.
        GraphCleanupPersistenceError: a graph could not be read.
    """
    return _plan_split(graph, baselines, x_id).preview


@dataclass(frozen=True, slots=True)
class CapabilitySplitApproval:
    """A previewed Capability split and the pending passkey approval bound to it.

    Attributes:
        preview: The split preview the officer saw.
        pending_approval_id: Id of the pending approval row.
        approval_url: Link where the officer signs with a passkey; includes the one-time code.
        expires_at: ISO 8601 time after which the approval can no longer be signed.
    """

    preview: CapabilitySplitPreview
    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601


def create_capability_split_approval(
    graph: GraphHandle,
    baselines: Sequence[tuple[str, GraphHandle]],
    *,
    x_id: str,
    acknowledge_governance_change: bool,
    actor: tuple[str, str],
    base_url: str,
    store: PendingApprovalStore,
    emitter: LogEmitter | None = None,
) -> CapabilitySplitApproval:
    """Preview the split and create a passkey approval bound to X, A and the previewed state.

    The signed `normalized_args` carry X, A, the acknowledgment flag and the sha256
    `state_digest` of the previewed state, so the signature covers the split the officer saw.

    Business Rules:
        When A has a governing Policy, the Obligations whose `REQUIRES` edge moves to X leave
        that Policy's coverage, so the officer's explicit acknowledgment is needed in addition to
        the passkey: without `acknowledge_governance_change` no approval is created.

    Args:
        graph: Handle on the single-tenant graph, read only.
        baselines: `(graph name, read handle)` of every `{short}_baseline` graph, read only.
        x_id: Id of the `ResolvedCapability` to split.
        acknowledge_governance_change: True when the officer accepts the governance change.
        actor: `(subject, issuer)` of the requesting officer; the approval is bound to them.
        base_url: Origin used to build the approval link, without a trailing slash.
        store: Pending-approval store that holds the approval row.
        emitter: Optional log emitter; None uses the default.

    Returns:
        The preview with the approval id, signing URL and expiry.

    Raises:
        GraphCleanupValidationError: the id cannot be split (see `preview_capability_split`); no
            approval is created.
        GraphCleanupSplitAcknowledgmentRequiredError: A is governed and the acknowledgment is
            missing; carries the preview, no approval is created.
        GraphCleanupPersistenceError: a graph could not be read.

    Side Effects:
        Creates one pending approval row and emits a structured log entry. Writes nothing to the
        graph; a refused split creates no row.
    """
    plan = _plan_split(graph, baselines, x_id)
    preview = plan.preview
    governance = preview.governance
    if governance is not None and not acknowledge_governance_change:
        emit_log_entry(
            component=_COMPONENT,
            action="create_capability_split_approval",
            outcome="acknowledgment_required",
            extra={"x_id": x_id, "a_id": preview.a_id, "policy_id": governance.policy.id},
            emitter=emitter,
        )
        raise GraphCleanupSplitAcknowledgmentRequiredError(
            preview,
            f"{governance.acknowledgment_text} Repeat the call with "
            "acknowledge_governance_change=true to receive the approval link.",
        )
    row, code = store.create_pending_approval(
        tool_name=TOOL_SPLIT_CAPABILITY,
        normalized_args={
            "x_id": x_id,
            "a_id": preview.a_id,
            "acknowledge_governance_change": acknowledge_governance_change,
            "state_digest": plan.state_digest,
        },
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary=preview.model_dump(mode="json"),
    )
    sets = (preview.edges.requires, preview.edges.covers, preview.edges.mitigated_by)
    emit_log_entry(
        component=_COMPONENT,
        action="create_capability_split_approval",
        outcome="succeeded",
        extra={
            "approval_id": row.id,
            "x_id": x_id,
            "a_id": preview.a_id,
            "moves": sum(len(edge_set.moves) for edge_set in sets),
            "shared": sum(len(edge_set.shared) for edge_set in sets),
            "added": sum(len(edge_set.added) for edge_set in sets),
        },
        emitter=emitter,
    )
    return CapabilitySplitApproval(
        preview=preview,
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
    """Preview merging Obligation `absorbed_id` into `survivor_id` with no writes.

    Args:
        graph: Handle on the single-tenant graph, read only.
        survivor_id: Id of the Obligation that remains; must differ from `absorbed_id`.
        absorbed_id: Id of the Obligation that is deleted after its edges move to the survivor.

    Returns:
        The merge preview for the pair, including the shared Role.

    Raises:
        GraphCleanupValidationError: self-merge, a nonexistent side, or two Obligations under
            different Roles.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _plan_obligations(graph, survivor_id=survivor_id, absorbed_id=absorbed_id).preview


@dataclass(frozen=True, slots=True)
class ObligationMergeApproval:
    """A previewed Obligation merge and the pending passkey approval bound to it.

    Attributes:
        preview: The merge preview the officer saw.
        pending_approval_id: Id of the pending approval row.
        approval_url: Link where the officer signs with a passkey; includes the one-time code.
        expires_at: ISO 8601 time after which the approval can no longer be signed.
    """

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
    state.

    Args:
        graph: Handle on the single-tenant graph, read only.
        survivor_id: Id of the Obligation that remains; must differ from `absorbed_id`.
        absorbed_id: Id of the Obligation that is deleted after the merge.
        actor: `(subject, issuer)` of the requesting officer; the approval is bound to them.
        base_url: Origin used to build the approval link, without a trailing slash.
        store: Pending-approval store that holds the approval row.
        emitter: Optional log emitter; None uses the default.

    Returns:
        The preview with the approval id, signing URL and expiry.

    Raises:
        GraphCleanupValidationError: self-merge, a nonexistent side, a side not borne by exactly
            one Role, or two Obligations under different Roles; no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.

    Side Effects:
        Creates one pending approval row and emits a structured log entry. Writes nothing to the
        graph; a rejected merge creates no row.
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


def _plan_roles(graph: GraphHandle, *, survivor_id: str, absorbed_id: str) -> RoleMergePlan:
    state = read_role_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    return plan_role_merge(state)


def preview_role_merge(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str
) -> RoleMergePreview:
    """Preview merging Role `absorbed_id` into `survivor_id` with no writes.

    Args:
        graph: Handle on the single-tenant graph, read only.
        survivor_id: Id of the Role that remains; must differ from `absorbed_id`.
        absorbed_id: Id of the Role that is deleted after its Obligations move to the survivor.

    Returns:
        The merge preview for the pair, including the instrument defining both.

    Raises:
        GraphCleanupValidationError: the pair cannot be merged.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _plan_roles(graph, survivor_id=survivor_id, absorbed_id=absorbed_id).preview


@dataclass(frozen=True, slots=True)
class RoleMergeApproval:
    """A previewed Role merge and the pending passkey approval bound to it.

    Attributes:
        preview: The merge preview the officer saw.
        pending_approval_id: Id of the pending approval row.
        approval_url: Link where the officer signs with a passkey; includes the one-time code.
        expires_at: ISO 8601 time after which the approval can no longer be signed.
    """

    preview: RoleMergePreview
    pending_approval_id: str
    approval_url: str
    expires_at: str  # ISO 8601


def create_role_merge_approval(
    graph: GraphHandle,
    *,
    survivor_id: str,
    absorbed_id: str,
    actor: tuple[str, str],
    base_url: str,
    store: PendingApprovalStore,
    review_id: str | None = None,
    emitter: LogEmitter | None = None,
) -> RoleMergeApproval:
    """Preview the role merge and create a passkey approval bound to the pair and state.

    The signed `normalized_args` carry both ids and the sha256 `state_digest` of the previewed
    state. A `review_id` is signed with them, must name a Role `PendingReview` of exactly this
    pair, and makes the executor delete that review in the merge group.

    Args:
        graph: Handle on the single-tenant graph, read only.
        survivor_id: Id of the Role that remains; must differ from `absorbed_id`.
        absorbed_id: Id of the Role that is deleted after the merge.
        actor: `(subject, issuer)` of the requesting officer; the approval is bound to them.
        base_url: Origin used to build the approval link, without a trailing slash.
        store: Pending-approval store that holds the approval row.
        review_id: Id of the Role `PendingReview` this merge resolves; None for a plain cleanup
            merge.
        emitter: Optional log emitter; None uses the default.

    Returns:
        The preview with the approval id, signing URL and expiry.

    Raises:
        GraphCleanupValidationError: the pair cannot be merged, or the review does not exist, is
            not a Role review or names another pair; no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.

    Side Effects:
        Creates one pending approval row and emits a structured log entry. Writes nothing to the
        graph; a rejected merge creates no row.
    """
    plan = _plan_roles(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    if review_id is not None:
        validate_role_review(
            read_pending_review(graph, review_id=review_id),
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
        )
    row, code = store.create_pending_approval(
        tool_name=TOOL_MERGE_ROLES,
        normalized_args={
            "survivor_id": survivor_id,
            "absorbed_id": absorbed_id,
            "state_digest": plan.state_digest,
            **({"review_id": review_id} if review_id is not None else {}),
        },
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary=plan.preview.model_dump(mode="json"),
    )
    emit_log_entry(
        component=_COMPONENT,
        action="create_role_merge_approval",
        outcome="succeeded",
        extra={
            "approval_id": row.id,
            "instrument_id": plan.preview.instrument_id,
            "review_id": review_id,
        },
        emitter=emitter,
    )
    return RoleMergeApproval(
        preview=plan.preview,
        pending_approval_id=row.id,
        approval_url=f"{base_url}/approvals/{row.id}#{code}",
        expires_at=row.expires_at.isoformat(),
    )


def _plan_release(graph: GraphHandle, *, capability_id: str) -> ReleasePlan:
    return plan_release_governance(read_release_state(graph, capability_id=capability_id))


def preview_release_governance(graph: GraphHandle, *, capability_id: str) -> ReleasePreview:
    """Preview releasing `capability_id` from its draft governing policy, with no writes.

    Args:
        graph: Handle on the single-tenant graph, read only.
        capability_id: Id of the governed Capability to release.

    Returns:
        The release preview: the governing policy and the Capabilities it governs.

    Raises:
        GraphCleanupValidationError: the Capability does not exist, is not active, is not
            governed, or its governing policy is not a draft.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _plan_release(graph, capability_id=capability_id).preview


@dataclass(frozen=True, slots=True)
class ReleaseGovernanceApproval:
    """A previewed governance release and the pending passkey approval bound to it.

    Attributes:
        preview: The release preview the officer saw.
        pending_approval_id: Id of the pending approval row.
        approval_url: Link where the officer signs with a passkey; includes the one-time code.
        expires_at: ISO 8601 time after which the approval can no longer be signed.
    """

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
    `state_digest` of the previewed state.

    Args:
        graph: Handle on the single-tenant graph, read only.
        capability_id: Id of the governed Capability to release.
        actor: `(subject, issuer)` of the requesting officer; the approval is bound to them.
        base_url: Origin used to build the approval link, without a trailing slash.
        store: Pending-approval store that holds the approval row.
        emitter: Optional log emitter; None uses the default.

    Returns:
        The preview with the approval id, signing URL and expiry.

    Raises:
        GraphCleanupValidationError: the Capability does not exist, is not active, is not
            governed, or its governing policy is not a draft; no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.

    Side Effects:
        Creates one pending approval row and emits a structured log entry. Writes nothing to the
        graph; a rejected release (any non-draft policy included) creates no row.
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
) -> CapabilityUnmergePreview | ObligationUnmergePreview | RoleUnmergePreview:
    """Preview reversing the newest effective merge of `merged_id`, with no writes.

    Args:
        graph: Handle on the single-tenant graph, read only.
        audit_store: Audit trail used to locate the merge to reverse.
        merged_id: Id of the absorbed Capability, Obligation or Role to restore.

    Returns:
        A Capability, Obligation or Role unmerge preview, matching the kind of node that was
        merged.

    Raises:
        GraphCleanupValidationError: there is no merge to reverse, or reversing it conflicts with
            the current graph; the message explains which.
        GraphCleanupPersistenceError: the graph database could not be read.
        AuditPostgresUnavailableError: the audit trail could not be read.
    """
    return plan_unmerge(graph, audit_store, merged_id=merged_id).preview


@dataclass(frozen=True, slots=True)
class UnmergeApproval:
    """A previewed unmerge and the pending passkey approval bound to it.

    Attributes:
        preview: The Capability, Obligation or Role unmerge preview the officer saw.
        pending_approval_id: Id of the pending approval row.
        approval_url: Link where the officer signs with a passkey; includes the one-time code.
        expires_at: ISO 8601 time after which the approval can no longer be signed.
    """

    preview: CapabilityUnmergePreview | ObligationUnmergePreview | RoleUnmergePreview
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
    reversed, the kind and the sha256 `state_digest` of the previewed state.

    Args:
        graph: Handle on the single-tenant graph, read only.
        audit_store: Audit trail used to locate the merge to reverse.
        merged_id: Id of the absorbed Capability, Obligation or Role to restore.
        actor: `(subject, issuer)` of the requesting officer; the approval is bound to them.
        base_url: Origin used to build the approval link, without a trailing slash.
        store: Pending-approval store that holds the approval row.
        emitter: Optional log emitter; None uses the default.

    Returns:
        The preview with the approval id, signing URL and expiry.

    Raises:
        GraphCleanupValidationError: there is no merge to reverse, or reversing it conflicts with
            the current graph; no approval is created.
        GraphCleanupPersistenceError: the graph database could not be read.
        AuditPostgresUnavailableError: the audit trail could not be read.

    Side Effects:
        Creates one pending approval row and emits a structured log entry. Writes nothing to the
        graph; a rejected or conflicting unmerge creates no row.
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
# Public: exported so callers and tests can reason about when reconciliation starts.
RECONCILE_GRACE = timedelta(seconds=300)

_INTERRUPTED_MESSAGE = (
    "the approved action did not take effect; "
    "if you still intend to proceed, ask for a new approval"
)


@dataclass(frozen=True, slots=True)
class CleanupApprovalStatus:
    """The caller's own cleanup approval: live status plus the stored outcome, if any.

    Attributes:
        pending_approval_id: Id of the approval row.
        tool_name: The cleanup tool the approval was created for.
        status: `signed` once signed; otherwise `expired` after `expires_at`, else `pending`.
        outcome: The executor's outcome dict (or the reconciler's), or None while none is recorded.
        review_id: Id of the near-miss `PendingReview` a `merge-capabilities` or `merge-roles`
            approval resolves; None for every other approval.
    """

    pending_approval_id: str
    tool_name: str
    status: Literal["pending", "expired", "signed"]
    outcome: dict[str, object] | None
    review_id: str | None = None


def _signed_review_id(row: PendingApprovalRow) -> str | None:
    """The near-miss review id signed into a `merge-capabilities` or `merge-roles` approval."""
    review_id = row.normalized_args.get("review_id")
    if row.tool_name in (TOOL_MERGE_CAPABILITIES, TOOL_MERGE_ROLES) and isinstance(review_id, str):
        return review_id
    return None


def live_status(row: PendingApprovalRow) -> Literal["pending", "expired", "signed"]:
    """The approval status: `signed`, else `expired` past `expires_at`, else `pending`.

    Derived on every read, never stored.
    """
    if row.status == "signed":
        return "signed"
    return "expired" if datetime.now(UTC) > row.expires_at else "pending"


type _OrphanRow = tuple[str, str, str, dict[str, object]]
"""Action, resource type, resource id and details of the `failed` row for an orphan."""


def _release_failed_row(row: PendingApprovalRow, reason: CleanupFailedReason) -> _OrphanRow:
    capability_id = str(row.normalized_args.get("capability_id", ""))
    details = CapabilityReleaseGovernanceDetails(
        capability_id=capability_id,
        policy_id=str(row.normalized_args.get("policy_id", "")),
        policy_status=str(row.display_summary.get("policy_status", "draft")),
        approval_id=row.id,
        reason_code=reason,
    ).model_dump()
    return CAPABILITY_RELEASE_GOVERNANCE_ACTION, "capability", capability_id, details


def _obligation_unmerge_failed_row(
    row: PendingApprovalRow, reason: CleanupFailedReason
) -> _OrphanRow:
    merged_id = str(row.normalized_args.get("merged_id", ""))
    details = ObligationUnmergeDetails(
        survivor_id=str(row.display_summary.get("survivor_id", "")),
        absorbed_id=merged_id,
        role_id=str(row.display_summary.get("role_id", "")),
        approval_id=row.id,
        reverses_approval_id=str(row.normalized_args.get("merge_approval_id", "")),
        reason_code=reason,
    ).model_dump()
    return OBLIGATION_UNMERGE_ACTION, "obligation", merged_id, details


def _capability_unmerge_failed_row(
    row: PendingApprovalRow, reason: CleanupFailedReason
) -> _OrphanRow:
    merged_id = str(row.normalized_args.get("merged_id", ""))
    details = CapabilityUnmergeDetails(
        survivor_id=str(row.display_summary.get("survivor_id", "")),
        absorbed_id=merged_id,
        approval_id=row.id,
        reverses_approval_id=str(row.normalized_args.get("merge_approval_id", "")),
        reason_code=reason,
    ).model_dump()
    return CAPABILITY_UNMERGE_ACTION, "capability", merged_id, details


def _role_unmerge_failed_row(row: PendingApprovalRow, reason: CleanupFailedReason) -> _OrphanRow:
    merged_id = str(row.normalized_args.get("merged_id", ""))
    details = RoleUnmergeDetails(
        survivor_id=str(row.display_summary.get("survivor_id", "")),
        absorbed_id=merged_id,
        approval_id=row.id,
        reverses_approval_id=str(row.normalized_args.get("merge_approval_id", "")),
        instrument_id=str(row.display_summary.get("instrument_id", "")) or None,
        reason_code=reason,
    ).model_dump()
    return ROLE_UNMERGE_ACTION, "role", merged_id, details


def _obligation_merge_failed_row(
    row: PendingApprovalRow, reason: CleanupFailedReason
) -> _OrphanRow:
    absorbed_id = str(row.normalized_args.get("absorbed_id", ""))
    details = ObligationMergeDetails(
        survivor_id=str(row.normalized_args.get("survivor_id", "")),
        absorbed_id=absorbed_id,
        role_id=str(row.display_summary.get("role_id", "")),
        approval_id=row.id,
        reason_code=reason,
    ).model_dump()
    return OBLIGATION_MERGE_ACTION, "obligation", absorbed_id, details


def _role_merge_failed_row(row: PendingApprovalRow, reason: CleanupFailedReason) -> _OrphanRow:
    absorbed_id = str(row.normalized_args.get("absorbed_id", ""))
    details = RoleMergeDetails(
        survivor_id=str(row.normalized_args.get("survivor_id", "")),
        absorbed_id=absorbed_id,
        approval_id=row.id,
        instrument_id=str(row.display_summary.get("instrument_id", "")),
        reason_code=reason,
        review_id=_signed_review_id(row),
    ).model_dump()
    return ROLE_MERGE_ACTION, "role", absorbed_id, details


def _capability_merge_failed_row(
    row: PendingApprovalRow, reason: CleanupFailedReason
) -> _OrphanRow:
    absorbed_id = str(row.normalized_args.get("absorbed_id", ""))
    case = row.display_summary.get("policy_case")
    details = CapabilityMergeDetails(
        survivor_id=str(row.normalized_args.get("survivor_id", "")),
        absorbed_id=absorbed_id,
        policy_case=case if case in _CASES_WITH_GOVERNANCE else 1,
        acknowledged=row.normalized_args.get("acknowledge_governance_change") is True,
        approval_id=row.id,
        reason_code=reason,
        review_id=_signed_review_id(row),
    ).model_dump()
    return CAPABILITY_MERGE_ACTION, "capability", absorbed_id, details


def _capability_split_failed_row(
    row: PendingApprovalRow, reason: CleanupFailedReason
) -> _OrphanRow:
    x_id = str(row.normalized_args.get("x_id", ""))
    details = CapabilitySplitDetails(
        x_id=x_id,
        a_id=str(row.normalized_args.get("a_id", "")),
        approval_id=row.id,
        acknowledged=row.normalized_args.get("acknowledge_governance_change") is True,
        policy_id=preview_policy_id(row.display_summary),
        reason_code=reason,
    ).model_dump()
    return CAPABILITY_SPLIT_ACTION, "capability", x_id, details


# Keyed by `(tool_name, kind)`; `kind` is the signed unmerge kind and empty for every other tool.
_FAILED_ROWS: dict[
    tuple[str, str], Callable[[PendingApprovalRow, CleanupFailedReason], _OrphanRow]
] = {
    (TOOL_RELEASE_GOVERNANCE, ""): _release_failed_row,
    (TOOL_UNMERGE, "obligation"): _obligation_unmerge_failed_row,
    (TOOL_UNMERGE, "capability"): _capability_unmerge_failed_row,
    (TOOL_UNMERGE, "role"): _role_unmerge_failed_row,
    (TOOL_MERGE_OBLIGATIONS, ""): _obligation_merge_failed_row,
    (TOOL_MERGE_ROLES, ""): _role_merge_failed_row,
    (TOOL_MERGE_CAPABILITIES, ""): _capability_merge_failed_row,
    (TOOL_SPLIT_CAPABILITY, ""): _capability_split_failed_row,
}


def failed_audit_row(row: PendingApprovalRow, reason: CleanupFailedReason) -> _OrphanRow:
    """Action, resource type, resource id and details of a `failed` row for a cleanup approval.

    The row is built from the approval alone (its signed arguments and display summary), so it
    names the action the approval was created for even when no plan exists. An unmerge whose kind
    is not recognised is treated as a Capability unmerge; an unknown tool as a Capability merge.

    Args:
        row: The approval the `failed` row is recorded for.
        reason: Why the approved edit did not happen.

    Returns:
        `(action, resource_type, resource_id, details)` ready for `record_standalone`.
    """
    kind = ""
    if row.tool_name == TOOL_UNMERGE:
        signed_kind = str(row.normalized_args.get("kind"))
        kind = signed_kind if (TOOL_UNMERGE, signed_kind) in _FAILED_ROWS else "capability"
    builder = _FAILED_ROWS.get((row.tool_name, kind), _capability_merge_failed_row)
    return builder(row, reason)


def interrupted_audit_row(row: PendingApprovalRow) -> _OrphanRow:
    """The `failed` row for an orphan: a signed approval whose effect is absent.

    Args:
        row: The orphaned approval.

    Returns:
        `failed_audit_row(row, "interrupted_no_effect")`.
    """
    return failed_audit_row(row, "interrupted_no_effect")


def graph_has_caught_up(
    row: PendingApprovalRow, *, config: ServiceConfig, dependencies: GraphCleanupDependencies
) -> bool:
    """Whether the single-tenant graph holds everything committed to its log; log a deferral if not.

    The question is asked of the gateway before the graph is opened. Every way of not knowing
    (behind the log, no gateway, the log unreadable) is a deferral, never an error: the state
    heals by itself and `check-cleanup-approval` is retried.
    """
    try:
        require_caught_up(graph_write_target(dependencies, config))
    except GraphCleanupPersistenceError as exc:
        reason = "graph_behind_its_log" if exc.__cause__ is None else "gateway_unavailable"
        emit_log_entry(
            component=_COMPONENT,
            action="reconcile_cleanup_approval",
            outcome="deferred",
            extra={"approval_id": row.id, "reason": reason},
        )
        return False
    return True


def _reconcile_orphan(
    row: PendingApprovalRow,
    *,
    store: PendingApprovalStore,
    config: ServiceConfig,
    dependencies: GraphCleanupDependencies,
) -> dict[str, object] | None:
    """Settle a signed row that has no outcome, past the grace window.

    Effect present -> outcome `{"reconciled": "applied"}`. Effect absent -> a `failed`
    audit row (`interrupted_no_effect`, same approval id) first, then a failed outcome.
    Anything that raises leaves the row untouched so a later check retries.

    The verifier reads the live graph, and a write is logged first: a committed group may
    not be applied to the graph yet, and "effect absent" read from a lagging graph would record a
    false `failed` row for an action that is committed. So the verifier runs only once the graph
    has caught up to its log. A graph behind its log, a gateway that is not installed, and a log
    that cannot be read all defer: `None`, no audit row, no outcome, nothing opened or read, and a
    `deferred` log entry; the next check retries.
    """
    verifier = resolve_effect_verifier(row.tool_name)
    if verifier is None:
        return None
    if not graph_has_caught_up(row, config=config, dependencies=dependencies):
        return None
    graph = dependencies.open_single_tenant_graph(config)
    if verifier(row, graph):
        outcome: dict[str, object] = {"reconciled": "applied"}
    else:
        action, resource_type, resource_id, details = interrupted_audit_row(row)
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
    """Return the caller's own cleanup approval, reconciling an orphaned signed row.

    A signed row with no outcome that is past `expires_at + RECONCILE_GRACE` is verified against
    the graph first, once the graph has caught up to its log; until then the row is returned
    with no outcome and a later check retries. See `_reconcile_orphan`.

    Args:
        pending_approval_id: Id of the approval to look up.
        actor: `(subject, issuer)` of the caller, or None when no actor could be resolved.
        store: Pending-approval store that holds the row and receives a reconciled outcome.
        config: Service configuration used to open the graph and the audit store.
        dependencies: Factories for the graph, audit store and gateway.

    Returns:
        The approval status. None for every case that must look like "not found": no resolvable
        actor, an unknown id, another actor's approval, or an approval that is not a cleanup
        approval.

    Raises:
        GraphCleanupPersistenceError: reconciliation could not read the graph.
        AuditPostgresUnavailableError: reconciliation could not record its `failed` row (the
            approval is left unreconciled).
        AuditPersistenceError: as above, for any other audit write failure.

    Side Effects:
        When reconciling, stores the outcome on the approval row and, if the approved effect is
        absent, records an `interrupted_no_effect` `failed` audit row. Emits log entries.
    """
    if actor is None:
        return None
    row = store.get_by_id(pending_approval_id)
    if row is None or (row.actor_subject, row.actor_issuer) != actor:
        return None
    if row.tool_name not in CLEANUP_TOOL_NAMES:
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
        status=live_status(row),
        outcome=outcome,
        review_id=_signed_review_id(row),
    )
