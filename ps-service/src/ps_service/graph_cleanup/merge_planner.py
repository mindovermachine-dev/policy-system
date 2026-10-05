"""Pure planning and validation for `merge-capabilities` (issue #190, AC-BI-005/010/016/021).

No I/O: takes the `MergeState` the reader produced and returns the preview, the
guard counts for the writer statement, the before/after snapshots and a state
digest. The digest binds a passkey approval to the exact state that was previewed.

Merge case 1 (neither governed), case 2 (exactly one governed: the `GOVERNED_BY` edge moves
with an absorbed governed capability, stays with a governed survivor) and case 3 (both governed
by the SAME policy: the absorbed edge is deleted, no acknowledgment) are planned. Both governed
by DIFFERENT policies is rejected before any approval (AC-BI-011) and points at
`release-capability-governance`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    CapabilityMergePlan,
    CapabilityMergePreview,
    CapabilityNodeState,
    EdgeRecord,
    EdgesToMove,
    ExpectedCounts,
    GovernanceChange,
    GoverningPolicy,
    GraphSnapshot,
    MergeState,
    NodeRecord,
)

__all__ = ["EDGE_CLASSES", "plan_capability_merge", "validate_capability_merge"]

# (relationship type, source label): every incoming edge class the merge moves.
EDGE_CLASSES: tuple[tuple[str, str], ...] = (
    ("REQUIRES", "Obligation"),
    ("COVERS", "PracticeArea"),
    ("MITIGATED_BY", "RiskPath"),
)

_RELEASE_TOOL = "release-capability-governance"


def _require_active(node: CapabilityNodeState, role: str) -> None:
    if node.status == "merged":
        message = f"the {role} capability {node.id!r} is a merged tombstone and cannot be merged"
        raise GraphCleanupValidationError(message)
    if node.status != "active":
        message = f"the {role} capability {node.id!r} is not active (status {node.status!r})"
        raise GraphCleanupValidationError(message)


def _different_policies_message(
    survivor: CapabilityNodeState,
    absorbed: CapabilityNodeState,
    survivor_policy: GoverningPolicy,
    absorbed_policy: GoverningPolicy,
) -> str:
    """The AC-BI-011 rejection: both policies, the release step, and what is actually possible."""
    intro = (
        f"cannot merge: capability {survivor.id!r} is governed by policy {survivor_policy.title!r} "
        f"({survivor_policy.id}, {survivor_policy.status}) and capability {absorbed.id!r} by "
        f"policy {absorbed_policy.title!r} ({absorbed_policy.id}, {absorbed_policy.status}); "
        "a merged capability can have only one governing policy."
    )
    if absorbed_policy.status == "draft":
        return (
            f"{intro} First release capability {absorbed.id!r} from policy "
            f"{absorbed_policy.id!r} with {_RELEASE_TOOL}, then merge again."
        )
    if survivor_policy.status == "draft":
        return (
            f"{intro} Policy {absorbed_policy.id!r} is {absorbed_policy.status}, so "
            f"{_RELEASE_TOOL} cannot release {absorbed.id!r}; swap survivor and absorbed, "
            f"release {survivor.id!r} from draft policy {survivor_policy.id!r} with "
            f"{_RELEASE_TOOL}, then merge again."
        )
    return (
        f"{intro} {_RELEASE_TOOL} works only on draft policies and neither policy is a draft, "
        "so there is currently no completion path: a policy fork carries the whole governed "
        "set and cannot drop one capability. A proposed policy can be returned to draft with "
        "revert-policy-to-draft; otherwise leave the capabilities separate."
    )


def validate_capability_merge(state: MergeState) -> tuple[CapabilityNodeState, CapabilityNodeState]:
    """Reject a merge that must never get an approval; return the (survivor, absorbed) nodes.

    Raises:
        GraphCleanupValidationError: self-merge, a nonexistent side, a `merged`
            tombstone or other non-active side, a side with more than one governing
            policy (a data fault), or both sides governed (case 3, not supported yet).
    """
    survivor, absorbed = state.survivor, state.absorbed
    if survivor is not None and absorbed is not None and survivor.id == absorbed.id:
        message = "a capability cannot be merged into itself"
        raise GraphCleanupValidationError(message)
    if survivor is None or absorbed is None:
        which = "survivor" if survivor is None else "absorbed"
        message = f"the {which} capability does not exist"
        raise GraphCleanupValidationError(message)
    _require_active(survivor, "survivor")
    _require_active(absorbed, "absorbed")
    if len(state.survivor_policies) > 1 or len(state.absorbed_policies) > 1:
        message = "a capability has more than one governing policy; this is a data fault"
        raise GraphCleanupValidationError(message)
    if (
        state.survivor_policies
        and state.absorbed_policies
        and state.survivor_policies[0].id != state.absorbed_policies[0].id
    ):
        raise GraphCleanupValidationError(
            _different_policies_message(
                survivor, absorbed, state.survivor_policies[0], state.absorbed_policies[0]
            )
        )
    return survivor, absorbed


def _snapshot_node(node: CapabilityNodeState, *, status: str) -> NodeRecord:
    properties = {**node.properties, "name": node.name, "status": status}
    return NodeRecord(label="Capability", id=node.id, properties=properties)


def _sorted_edges(edges: list[EdgeRecord]) -> tuple[EdgeRecord, ...]:
    return tuple(
        sorted(
            edges,
            key=lambda e: (e.rel_type, e.source_id, e.target_id, e.source_label, e.target_label),
        )
    )


def _digest(before: GraphSnapshot) -> str:
    canonical = json.dumps(before.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _acknowledgment_text(policy: GoverningPolicy, governed_side: str) -> str:
    moved = (
        "the absorbed capability's governing edge moves to the survivor"
        if governed_side == "absorbed"
        else "the absorbed capability's obligations are covered by the survivor's policy"
    )
    state = (
        "its governed set changes, its content/version does not"
        if policy.status == "approved"
        else "its governed set changes"
    )
    return (
        f"Merging changes what policy {policy.title!r} ({policy.id}) governs: {moved}. "
        f"The policy is `{policy.status}`: {state}."
    )


def _governance(state: MergeState, survivor_id: str, absorbed_id: str) -> GovernanceChange | None:
    """The governance change of a case-2 or case-3 merge, or `None` for case 1.

    Validation already rejected two different policies, so when both sides are governed
    they share one policy and nothing needs acknowledging.
    """
    if not state.survivor_policies and not state.absorbed_policies:
        return None
    both = bool(state.survivor_policies and state.absorbed_policies)
    absorbed_governed = bool(state.absorbed_policies)
    policy = state.absorbed_policies[0] if absorbed_governed else state.survivor_policies[0]
    before = tuple(sorted(state.governed_sets.get(policy.id, ())))
    if both:
        return GovernanceChange(
            policy=policy,
            governed_side="both",
            obligations_coverage_changed=0,
            governed_set_before=before,
            governed_set_after=tuple(sorted(set(before) - {absorbed_id})),
            acknowledgment_required=False,
            acknowledgment_text=_same_policy_text(policy),
        )
    governed_id, ungoverned_id = (
        (absorbed_id, survivor_id) if absorbed_governed else (survivor_id, absorbed_id)
    )
    requiring = {
        capability_id: {
            e.source_id
            for e in state.edges
            if e.rel_type == "REQUIRES" and e.target_id == capability_id
        }
        for capability_id in (governed_id, ungoverned_id)
    }
    after = tuple(sorted((set(before) - {absorbed_id}) | {survivor_id}))
    side = "absorbed" if absorbed_governed else "survivor"
    return GovernanceChange(
        policy=policy,
        governed_side=side,
        obligations_coverage_changed=len(requiring[ungoverned_id] - requiring[governed_id]),
        governed_set_before=before,
        governed_set_after=after if absorbed_governed else before,
        acknowledgment_required=True,
        acknowledgment_text=_acknowledgment_text(policy, side),
    )


def _same_policy_text(policy: GoverningPolicy) -> str:
    return (
        f"Both capabilities are already governed by policy {policy.title!r} ({policy.id}, "
        f"`{policy.status}`): the absorbed capability leaves its governed set and the "
        "survivor stays in it. No acknowledgment is needed."
    )


def _governed_by_edge(capability_id: str, policy_id: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="GOVERNED_BY",
        source_label="Capability",
        source_id=capability_id,
        target_label="Policy",
        target_id=policy_id,
    )


def _expected(
    state: MergeState, moved_requires: int, moved_covers: int, moved_mitigated: int
) -> ExpectedCounts:
    absorbed_policy = state.absorbed_policies[0] if state.absorbed_policies else None
    survivor_policy = state.survivor_policies[0] if state.survivor_policies else None
    return ExpectedCounts(
        requires=moved_requires,
        covers=moved_covers,
        mitigated=moved_mitigated,
        absorbed_governed=len(state.absorbed_policies),
        survivor_governed=len(state.survivor_policies),
        absorbed_policy_id=absorbed_policy.id if absorbed_policy else None,
        absorbed_policy_status=absorbed_policy.status if absorbed_policy else None,
        survivor_policy_id=survivor_policy.id if survivor_policy else None,
        survivor_policy_status=survivor_policy.status if survivor_policy else None,
    )


def _policy_case(state: MergeState) -> Literal[1, 2, 3]:
    governed = bool(state.survivor_policies) + bool(state.absorbed_policies)
    return 1 if governed == 0 else 2 if governed == 1 else 3


def plan_capability_merge(state: MergeState) -> CapabilityMergePlan:
    """Plan a case-1, case-2 or case-3 (same policy) capability merge from `state` (no I/O).

    Every `REQUIRES` / `COVERS` / `MITIGATED_BY` edge of the absorbed Capability moves
    onto the survivor; an endpoint already linked to the survivor collapses into the
    existing edge (no duplicate). The absorbed node is kept as a `merged` tombstone with
    a `MERGED_INTO` edge to the survivor. In case 2 the governing `GOVERNED_BY` edge of an
    absorbed governed capability moves to the survivor; a governed survivor keeps its own.

    Raises:
        GraphCleanupValidationError: see `validate_capability_merge`.
    """
    survivor, absorbed = validate_capability_merge(state)
    incident = [e for e in state.edges if e.target_id in {survivor.id, absorbed.id}]
    moved: dict[str, list[str]] = {}
    collapsed = 0
    after_edges: list[EdgeRecord] = []
    for rel, source_label in EDGE_CLASSES:
        on_survivor = {
            e.source_id for e in incident if e.rel_type == rel and e.target_id == survivor.id
        }
        on_absorbed = sorted(
            {e.source_id for e in incident if e.rel_type == rel and e.target_id == absorbed.id}
        )
        moved[rel] = on_absorbed
        collapsed += sum(1 for source in on_absorbed if source in on_survivor)
        after_edges.extend(
            EdgeRecord(
                rel_type=rel,
                source_label=source_label,
                source_id=source,
                target_label="Capability",
                target_id=survivor.id,
            )
            for source in sorted(on_survivor | set(on_absorbed))
        )
    after_edges.append(
        EdgeRecord(
            rel_type="MERGED_INTO",
            source_label="Capability",
            source_id=absorbed.id,
            target_label="Capability",
            target_id=survivor.id,
        )
    )
    governance = _governance(state, survivor.id, absorbed.id)
    policies = tuple(
        {p.id: p for p in (*state.survivor_policies, *state.absorbed_policies)}.values()
    )
    policy_nodes = tuple(
        NodeRecord(label="Policy", id=p.id, properties={"title": p.title, "status": p.status})
        for p in policies
    )
    governed_before = [_governed_by_edge(survivor.id, p.id) for p in state.survivor_policies] + [
        _governed_by_edge(absorbed.id, p.id) for p in state.absorbed_policies
    ]
    after_edges.extend(_governed_by_edge(survivor.id, p.id) for p in policies)
    before = GraphSnapshot(
        nodes=(
            _snapshot_node(survivor, status=survivor.status),
            _snapshot_node(absorbed, status=absorbed.status),
            *policy_nodes,
        ),
        edges=_sorted_edges([*incident, *governed_before]),
    )
    after = GraphSnapshot(
        nodes=(
            _snapshot_node(survivor, status=survivor.status),
            _snapshot_node(absorbed, status="merged"),
            *policy_nodes,
        ),
        edges=_sorted_edges(after_edges),
    )
    digest = _digest(before)
    preview = CapabilityMergePreview(
        survivor_id=survivor.id,
        survivor_name=survivor.name,
        absorbed_id=absorbed.id,
        absorbed_name=absorbed.name,
        policy_case=_policy_case(state),
        edges_to_move=EdgesToMove(
            requires=tuple(moved["REQUIRES"]),
            covers=tuple(moved["COVERS"]),
            mitigated_by=tuple(moved["MITIGATED_BY"]),
        ),
        duplicate_edges_collapsed=collapsed,
        obligations_affected=len(moved["REQUIRES"]),
        state_digest=digest,
        governance=governance,
    )
    expected = _expected(
        state, len(moved["REQUIRES"]), len(moved["COVERS"]), len(moved["MITIGATED_BY"])
    )
    return CapabilityMergePlan(
        preview=preview, expected=expected, before=before, after=after, state_digest=digest
    )
