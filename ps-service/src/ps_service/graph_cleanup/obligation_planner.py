"""Pure planning and validation for `merge-obligations` (issue #190, AC-BI-005/007/015/016).

No I/O: takes the `ObligationMergeState` the reader produced and returns the preview, the
guard counts for the writer statement, the before/after snapshots and a state digest. The
digest binds a passkey approval to the exact state that was previewed.

Both sides must be borne by the same single Role (an Obligation is a Role-scoped weak
entity). The survivor keeps its single `HAS` edge; the absorbed node is deleted, its
`SATISFIED_BY` / `REQUIRES` edges union onto the survivor, and a `MergedObligation` marker
records the redirect (CHANGES.md H1).
"""

from __future__ import annotations

import hashlib
import json

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.models import (
    EdgeRecord,
    GraphSnapshot,
    NodeRecord,
    ObligationEdgesToMove,
    ObligationExpectedCounts,
    ObligationMergePlan,
    ObligationMergePreview,
    ObligationMergeState,
    ObligationNodeState,
    RoleRef,
)

__all__ = ["plan_obligation_merge", "validate_obligation_merge"]


def _single_role(roles: tuple[RoleRef, ...], side: str, node_id: str) -> RoleRef:
    if len(roles) != 1:
        message = f"the {side} obligation {node_id!r} is not borne by exactly one role"
        raise GraphCleanupValidationError(message)
    return roles[0]


def validate_obligation_merge(
    state: ObligationMergeState,
) -> tuple[ObligationNodeState, ObligationNodeState, RoleRef]:
    """Reject a merge that must never get an approval; return (survivor, absorbed, role).

    Raises:
        GraphCleanupValidationError: self-merge, a nonexistent side, a side not borne by
            exactly one Role, or two Obligations under different Roles.
    """
    survivor, absorbed = state.survivor, state.absorbed
    if survivor is not None and absorbed is not None and survivor.id == absorbed.id:
        message = "an obligation cannot be merged into itself"
        raise GraphCleanupValidationError(message)
    if survivor is None or absorbed is None:
        which = "survivor" if survivor is None else "absorbed"
        message = f"the {which} obligation does not exist"
        raise GraphCleanupValidationError(message)
    survivor_role = _single_role(state.survivor_roles, "survivor", survivor.id)
    absorbed_role = _single_role(state.absorbed_roles, "absorbed", absorbed.id)
    if survivor_role.id != absorbed_role.id:
        message = (
            "obligations under different roles cannot be merged: the survivor is borne by "
            f"{survivor_role.name!r} ({survivor_role.id}), the absorbed by "
            f"{absorbed_role.name!r} ({absorbed_role.id})"
        )
        raise GraphCleanupValidationError(message)
    return survivor, absorbed, survivor_role


def _snapshot_node(node: ObligationNodeState) -> NodeRecord:
    return NodeRecord(
        label="Obligation", id=node.id, properties={**node.properties, "text": node.text}
    )


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


def _has_edge(role: RoleRef, obligation_id: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="HAS",
        source_label="Role",
        source_id=role.id,
        target_label="Obligation",
        target_id=obligation_id,
    )


def plan_obligation_merge(state: ObligationMergeState) -> ObligationMergePlan:
    """Plan an obligation merge from the read `state` (no I/O).

    Every `SATISFIED_BY` and `REQUIRES` edge of the absorbed Obligation unions onto the
    survivor (an endpoint already linked to the survivor collapses into the existing edge).

    Raises:
        GraphCleanupValidationError: see `validate_obligation_merge`.
    """
    survivor, absorbed, role = validate_obligation_merge(state)
    incident = [
        e
        for e in state.edges
        if survivor.id in {e.source_id, e.target_id} or absorbed.id in {e.source_id, e.target_id}
    ]
    sat_on_absorbed = sorted(
        {
            e.source_id
            for e in incident
            if e.rel_type == "SATISFIED_BY" and e.target_id == absorbed.id
        }
    )
    sat_on_survivor = {
        e.source_id for e in incident if e.rel_type == "SATISFIED_BY" and e.target_id == survivor.id
    }
    req_on_absorbed = sorted(
        {e.target_id for e in incident if e.rel_type == "REQUIRES" and e.source_id == absorbed.id}
    )
    req_on_survivor = {
        e.target_id for e in incident if e.rel_type == "REQUIRES" and e.source_id == survivor.id
    }
    collapsed = sum(1 for r in sat_on_absorbed if r in sat_on_survivor) + sum(
        1 for c in req_on_absorbed if c in req_on_survivor
    )
    after_edges = [_has_edge(role, survivor.id)]
    after_edges.extend(
        EdgeRecord(
            rel_type="SATISFIED_BY",
            source_label="Requirement",
            source_id=requirement_id,
            target_label="Obligation",
            target_id=survivor.id,
        )
        for requirement_id in sorted(sat_on_survivor | set(sat_on_absorbed))
    )
    after_edges.extend(
        EdgeRecord(
            rel_type="REQUIRES",
            source_label="Obligation",
            source_id=survivor.id,
            target_label="Capability",
            target_id=capability_id,
        )
        for capability_id in sorted(req_on_survivor | set(req_on_absorbed))
    )
    before = GraphSnapshot(
        nodes=(_snapshot_node(survivor), _snapshot_node(absorbed)),
        edges=_sorted_edges(
            [_has_edge(role, survivor.id), _has_edge(role, absorbed.id), *incident]
        ),
    )
    after = GraphSnapshot(
        nodes=(
            _snapshot_node(survivor),
            NodeRecord(
                label="MergedObligation", id=absorbed.id, properties={"merged_into": survivor.id}
            ),
        ),
        edges=_sorted_edges(after_edges),
    )
    digest = _digest(before)
    preview = ObligationMergePreview(
        survivor_id=survivor.id,
        survivor_text=survivor.text,
        absorbed_id=absorbed.id,
        absorbed_text=absorbed.text,
        role_id=role.id,
        role_name=role.name,
        edges_to_move=ObligationEdgesToMove(
            satisfied_by=tuple(sat_on_absorbed), requires=tuple(req_on_absorbed)
        ),
        duplicate_edges_collapsed=collapsed,
        requirement_source_refs=state.absorbed_requirement_refs,
        state_digest=digest,
    )
    expected = ObligationExpectedCounts(
        satisfied=len(sat_on_absorbed), requires=len(req_on_absorbed)
    )
    return ObligationMergePlan(
        preview=preview, expected=expected, before=before, after=after, state_digest=digest
    )
