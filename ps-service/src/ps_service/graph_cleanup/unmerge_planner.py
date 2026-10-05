"""Pure planning and conflict detection for capability `unmerge` (issue #190, AC-BI-019/020/021).

No I/O. `derive_capability_unmerge_inputs` reads what a `capability.merge` audit snapshot says
to reverse; `plan_capability_unmerge` compares it with the live graph, rejects every conflict
with an explanation (D9), and returns the preview, the exact parameters the guarded writer
statement pins, the before/after snapshots and a state digest binding the approval.

Only the edges the merge moved come back, and only those the merge moved leave the survivor;
anything the survivor gained since stays in place and is listed (`survivor_added_edges`).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.merge_planner import EDGE_CLASSES
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    CapabilityUnmergeInputs,
    CapabilityUnmergePlan,
    CapabilityUnmergePreview,
    CapabilityUnmergeState,
    CapabilityUnmergeWrite,
    EdgeRecord,
    GraphSnapshot,
    NodeRecord,
    ObligationUnmergeEdgeSet,
    ObligationUnmergeInputs,
    ObligationUnmergePlan,
    ObligationUnmergePreview,
    ObligationUnmergeState,
    ObligationUnmergeWrite,
    UnmergeEdgeSet,
)

if TYPE_CHECKING:
    from ps_service.graph_cleanup.audit_actions import (
        CapabilityMergeDetails,
        ObligationMergeDetails,
    )

__all__ = [
    "derive_capability_unmerge_inputs",
    "derive_obligation_unmerge_inputs",
    "plan_capability_unmerge",
    "plan_obligation_unmerge",
]

_REL_FIELDS = {"REQUIRES": "requires", "COVERS": "covers", "MITIGATED_BY": "mitigated"}
_SOURCE_LABELS = dict(EDGE_CLASSES)


def derive_capability_unmerge_inputs(details: CapabilityMergeDetails) -> CapabilityUnmergeInputs:
    """Derive what to restore and remove from a merge's `before`/`after` snapshots.

    Raises:
        GraphCleanupValidationError: the audit record carries no snapshot.
    """
    if details.before is None or details.after is None:
        message = "the merge's audit record carries no snapshot, so it cannot be reversed"
        raise GraphCleanupValidationError(message)
    survivor_id, absorbed_id = details.survivor_id, details.absorbed_id
    restore: dict[str, tuple[str, ...]] = {}
    remove: dict[str, tuple[str, ...]] = {}
    for rel in _REL_FIELDS:
        on_absorbed = sorted(
            {
                e.source_id
                for e in details.before.edges
                if e.rel_type == rel and e.target_id == absorbed_id
            }
        )
        on_survivor = {
            e.source_id
            for e in details.before.edges
            if e.rel_type == rel and e.target_id == survivor_id
        }
        restore[rel] = tuple(on_absorbed)
        remove[rel] = tuple(source for source in on_absorbed if source not in on_survivor)
    absorbed_policy = next(
        (
            e.target_id
            for e in details.before.edges
            if e.rel_type == "GOVERNED_BY" and e.source_id == absorbed_id
        ),
        None,
    )
    survivor_was_governed = any(
        e.rel_type == "GOVERNED_BY" and e.source_id == survivor_id for e in details.before.edges
    )
    snapshot_survivor_edges = tuple(
        e for e in details.after.edges if e.rel_type in _REL_FIELDS and e.target_id == survivor_id
    )
    return CapabilityUnmergeInputs(
        merge_approval_id=details.approval_id,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        policy_case=details.policy_case,
        restore_requires=restore["REQUIRES"],
        restore_covers=restore["COVERS"],
        restore_mitigated=restore["MITIGATED_BY"],
        remove_requires=remove["REQUIRES"],
        remove_covers=remove["COVERS"],
        remove_mitigated=remove["MITIGATED_BY"],
        restore_policy_id=absorbed_policy,
        remove_policy_edge=absorbed_policy is not None and not survivor_was_governed,
        snapshot_survivor_edges=snapshot_survivor_edges,
    )


def _restore_ids(inputs: CapabilityUnmergeInputs) -> dict[str, tuple[str, ...]]:
    return {
        "REQUIRES": inputs.restore_requires,
        "COVERS": inputs.restore_covers,
        "MITIGATED_BY": inputs.restore_mitigated,
    }


def _remove_candidates(inputs: CapabilityUnmergeInputs) -> dict[str, tuple[str, ...]]:
    return {
        "REQUIRES": inputs.remove_requires,
        "COVERS": inputs.remove_covers,
        "MITIGATED_BY": inputs.remove_mitigated,
    }


def _check_tombstone(inputs: CapabilityUnmergeInputs, state: CapabilityUnmergeState) -> None:
    absorbed, survivor_id = state.absorbed, inputs.survivor_id
    if absorbed is None:
        message = f"capability {inputs.absorbed_id!r} no longer exists, so it cannot be unmerged"
        raise GraphCleanupValidationError(message)
    if absorbed.status != "merged":
        message = (
            f"capability {absorbed.id!r} is not a merged tombstone (status {absorbed.status!r}); "
            "there is nothing to unmerge"
        )
        raise GraphCleanupValidationError(message)
    targets = state.redirects.get(absorbed.id, ())
    if survivor_id in targets:
        return
    if targets:
        message = (
            f"capability {absorbed.id!r} was merged into {survivor_id!r}, but its redirect now "
            f"points at {', '.join(repr(t) for t in targets)} (a later merge re-pointed it), "
            "so reversing the original merge would conflict"
        )
    else:
        message = (
            f"capability {absorbed.id!r} is marked merged but has no MERGED_INTO edge; "
            "its state is inconsistent and it cannot be unmerged safely"
        )
    raise GraphCleanupValidationError(message)


def _check_survivor(inputs: CapabilityUnmergeInputs, state: CapabilityUnmergeState) -> None:
    survivor = state.survivor
    if survivor is None:
        message = (
            f"the survivor capability {inputs.survivor_id!r} no longer exists, "
            "so the merge cannot be reversed"
        )
        raise GraphCleanupValidationError(message)
    if survivor.status == "active":
        return
    moved_to = state.redirects.get(survivor.id, ())
    where = f" into {', '.join(repr(t) for t in moved_to)}" if moved_to else ""
    message = (
        f"the survivor capability {survivor.id!r} is no longer active (status "
        f"{survivor.status!r}; it was merged{where} after this merge), so the merge cannot be "
        "reversed"
    )
    raise GraphCleanupValidationError(message)


def _check_endpoints(inputs: CapabilityUnmergeInputs, state: CapabilityUnmergeState) -> None:
    missing = sorted(
        {
            endpoint
            for rel, ids in _restore_ids(inputs).items()
            for endpoint in ids
            if endpoint not in state.existing_endpoints.get(rel, ())
        }
    )
    if missing:
        message = (
            f"{', '.join(repr(m) for m in missing)} no longer exists (it may have been merged "
            "into another node or removed), so its edge cannot be restored"
        )
        raise GraphCleanupValidationError(message)


def _check_governance(inputs: CapabilityUnmergeInputs, state: CapabilityUnmergeState) -> None:
    policy_id = inputs.restore_policy_id
    if policy_id is None:
        return
    if not state.policy_exists:
        message = f"policy {policy_id!r} that governed {inputs.absorbed_id!r} no longer exists"
        raise GraphCleanupValidationError(message)
    if tuple(sorted(state.survivor_policy_ids)) != (policy_id,):
        now = ", ".join(repr(p) for p in sorted(state.survivor_policy_ids)) or "no policy"
        message = (
            f"the survivor's governance changed since the merge: it was governed by policy "
            f"{policy_id!r} and is now governed by {now}; restoring {inputs.absorbed_id!r} "
            "under that policy would conflict"
        )
        raise GraphCleanupValidationError(message)


def _edge(rel: str, source_id: str, target_id: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type=rel,
        source_label=_SOURCE_LABELS[rel],
        source_id=source_id,
        target_label="Capability",
        target_id=target_id,
    )


def _governed(capability_id: str, policy_id: str) -> EdgeRecord:
    return EdgeRecord(
        rel_type="GOVERNED_BY",
        source_label="Capability",
        source_id=capability_id,
        target_label="Policy",
        target_id=policy_id,
    )


def _sorted_edges(edges: list[EdgeRecord]) -> tuple[EdgeRecord, ...]:
    return tuple(
        sorted(
            edges,
            key=lambda e: (e.rel_type, e.source_id, e.target_id, e.source_label, e.target_label),
        )
    )


def _snapshot_node(node: CapabilityNodeState, *, status: str) -> NodeRecord:
    return NodeRecord(
        label="Capability",
        id=node.id,
        properties={**node.properties, "name": node.name, "status": status},
    )


def _digest(
    before: GraphSnapshot, inputs: CapabilityUnmergeInputs, write: CapabilityUnmergeWrite
) -> str:
    canonical = json.dumps(
        {
            "before": before.model_dump(mode="json"),
            "merge_approval_id": inputs.merge_approval_id,
            "write": write.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _edge_set(by_rel: dict[str, tuple[str, ...]], policy_id: str | None) -> UnmergeEdgeSet:
    return UnmergeEdgeSet(
        requires=by_rel["REQUIRES"],
        covers=by_rel["COVERS"],
        mitigated_by=by_rel["MITIGATED_BY"],
        governed_by=policy_id,
    )


def plan_capability_unmerge(
    inputs: CapabilityUnmergeInputs, state: CapabilityUnmergeState
) -> CapabilityUnmergePlan:
    """Plan reversing one capability merge against the live graph `state` (no I/O).

    Raises:
        GraphCleanupValidationError: the tombstone is missing, not merged or re-pointed, the
            survivor is gone or no longer active, a snapshot endpoint is gone, or the survivor's
            governance changed in a way that makes the restore contradictory (AC-BI-020).
    """
    _check_tombstone(inputs, state)
    _check_survivor(inputs, state)
    _check_endpoints(inputs, state)
    _check_governance(inputs, state)
    absorbed, survivor = state.absorbed, state.survivor
    if absorbed is None or survivor is None:  # pragma: no cover -- narrowed by the checks above
        message = "the capabilities no longer exist"
        raise GraphCleanupValidationError(message)
    restore = _restore_ids(inputs)
    current: dict[str, set[str]] = {rel: set() for rel in _REL_FIELDS}
    for edge in state.survivor_edges:
        if edge.rel_type in current and edge.target_id == survivor.id:
            current[edge.rel_type].add(edge.source_id)
    remove = {
        rel: tuple(source for source in candidates if source in current[rel])
        for rel, candidates in _remove_candidates(inputs).items()
    }
    write = CapabilityUnmergeWrite(
        restore_requires_ids=restore["REQUIRES"],
        remove_requires_ids=remove["REQUIRES"],
        restore_covers_ids=restore["COVERS"],
        remove_covers_ids=remove["COVERS"],
        restore_mitigated_ids=restore["MITIGATED_BY"],
        remove_mitigated_ids=remove["MITIGATED_BY"],
        restore_policy_id=inputs.restore_policy_id,
        remove_policy_edge=inputs.remove_policy_edge,
    )
    snapshot_keys = {(e.rel_type, e.source_id) for e in inputs.snapshot_survivor_edges}
    added = _sorted_edges(
        [
            e
            for e in state.survivor_edges
            if e.rel_type in _REL_FIELDS
            and e.target_id == survivor.id
            and (e.rel_type, e.source_id) not in snapshot_keys
        ]
    )
    restored = _sorted_edges(
        [_edge(rel, source, absorbed.id) for rel, ids in restore.items() for source in ids]
        + ([_governed(absorbed.id, inputs.restore_policy_id)] if inputs.restore_policy_id else [])
    )
    survivor_before = [e for e in state.survivor_edges if e.rel_type in _REL_FIELDS]
    survivor_governed_before = [_governed(survivor.id, p) for p in state.survivor_policy_ids]
    removed_keys = {(rel, source) for rel, ids in remove.items() for source in ids}
    survivor_after = [e for e in survivor_before if (e.rel_type, e.source_id) not in removed_keys]
    survivor_governed_after = [
        edge
        for edge in survivor_governed_before
        if not (inputs.remove_policy_edge and edge.target_id == inputs.restore_policy_id)
    ]
    policy_nodes = (
        (NodeRecord(label="Policy", id=inputs.restore_policy_id, properties={}),)
        if inputs.restore_policy_id
        else ()
    )
    before = GraphSnapshot(
        nodes=(
            _snapshot_node(absorbed, status="merged"),
            _snapshot_node(survivor, status=survivor.status),
            *policy_nodes,
        ),
        edges=_sorted_edges(
            [
                EdgeRecord(
                    rel_type="MERGED_INTO",
                    source_label="Capability",
                    source_id=absorbed.id,
                    target_label="Capability",
                    target_id=survivor.id,
                ),
                *survivor_before,
                *survivor_governed_before,
            ]
        ),
    )
    after = GraphSnapshot(
        nodes=(
            _snapshot_node(absorbed, status="active"),
            _snapshot_node(survivor, status=survivor.status),
            *policy_nodes,
        ),
        edges=_sorted_edges([*restored, *survivor_after, *survivor_governed_after]),
    )
    digest = _digest(before, inputs, write)
    preview = CapabilityUnmergePreview(
        merged_id=absorbed.id,
        merged_name=absorbed.name,
        survivor_id=survivor.id,
        survivor_name=survivor.name,
        merge_approval_id=inputs.merge_approval_id,
        edges_to_restore=_edge_set(restore, inputs.restore_policy_id),
        edges_removed_from_survivor=_edge_set(
            remove, inputs.restore_policy_id if inputs.remove_policy_edge else None
        ),
        survivor_added_edges=added,
        state_digest=digest,
    )
    return CapabilityUnmergePlan(
        preview=preview,
        write=write,
        restored_edges=restored,
        before=before,
        after=after,
        state_digest=digest,
        inputs=inputs,
    )


_OBLIGATION_NOTE = (
    "Edges the survivor holds that came from the absorbed obligation cannot be told apart from "
    "its own, so they stay on the survivor; those listed as survivor_edges_possibly_from_merge "
    "may originate from the merge."
)


def derive_obligation_unmerge_inputs(details: ObligationMergeDetails) -> ObligationUnmergeInputs:
    """Derive the node and edges to recreate from an obligation merge's snapshots.

    Raises:
        GraphCleanupValidationError: the audit record carries no snapshot or no absorbed node.
    """
    if details.before is None or details.after is None:
        message = "the merge's audit record carries no snapshot, so it cannot be reversed"
        raise GraphCleanupValidationError(message)
    survivor_id, absorbed_id = details.survivor_id, details.absorbed_id
    node = next(
        (n for n in details.before.nodes if n.label == "Obligation" and n.id == absorbed_id), None
    )
    if node is None:
        message = (
            "the merge's audit record does not hold the deleted obligation, "
            "so it cannot be reversed"
        )
        raise GraphCleanupValidationError(message)

    def _survivor_edges(edges: tuple[EdgeRecord, ...]) -> tuple[EdgeRecord, ...]:
        return _sorted_edges(
            [
                e
                for e in edges
                if (e.rel_type == "SATISFIED_BY" and e.target_id == survivor_id)
                or (e.rel_type == "REQUIRES" and e.source_id == survivor_id)
            ]
        )

    return ObligationUnmergeInputs(
        merge_approval_id=details.approval_id,
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        role_id=details.role_id,
        properties=dict(node.properties),
        satisfied_by_ids=tuple(
            sorted(
                {
                    e.source_id
                    for e in details.before.edges
                    if e.rel_type == "SATISFIED_BY" and e.target_id == absorbed_id
                }
            )
        ),
        requires_ids=tuple(
            sorted(
                {
                    e.target_id
                    for e in details.before.edges
                    if e.rel_type == "REQUIRES" and e.source_id == absorbed_id
                }
            )
        ),
        snapshot_survivor_edges=_survivor_edges(details.after.edges),
        survivor_edges_before_merge=_survivor_edges(details.before.edges),
    )


def _check_obligation_state(inputs: ObligationUnmergeInputs, state: ObligationUnmergeState) -> None:
    absorbed_id, survivor_id = inputs.absorbed_id, inputs.survivor_id
    if state.absorbed_exists:
        message = f"obligation {absorbed_id!r} already exists again; there is nothing to unmerge"
        raise GraphCleanupValidationError(message)
    targets = state.marker_targets.get(absorbed_id, ())
    if survivor_id not in targets:
        if targets:
            message = (
                f"obligation {absorbed_id!r} was merged into {survivor_id!r}, but its "
                f"MergedObligation marker now points at {', '.join(repr(t) for t in targets)}, "
                "so reversing the original merge would conflict"
            )
        else:
            message = (
                f"obligation {absorbed_id!r} has no MergedObligation marker, so it is not a "
                "merged obligation that can be unmerged"
            )
        raise GraphCleanupValidationError(message)
    if not state.survivor_exists:
        moved_to = state.marker_targets.get(survivor_id, ())
        where = f" (it was merged into {', '.join(repr(t) for t in moved_to)})" if moved_to else ""
        message = (
            f"the survivor obligation {survivor_id!r} no longer exists{where}, "
            "so the merge cannot be reversed"
        )
        raise GraphCleanupValidationError(message)
    if not state.role_exists:
        message = f"role {inputs.role_id!r} that bore the obligation no longer exists"
        raise GraphCleanupValidationError(message)
    missing_requirements = [
        r for r in inputs.satisfied_by_ids if r not in state.existing_requirements
    ]
    gone = [c for c in inputs.requires_ids if c not in state.capability_statuses]
    tombstones = [c for c in inputs.requires_ids if state.capability_statuses.get(c) == "merged"]
    problems = [f"requirement {r!r} no longer exists" for r in missing_requirements]
    problems += [f"capability {c!r} no longer exists" for c in gone]
    problems += [f"capability {c!r} is now a merged tombstone" for c in tombstones]
    if problems:
        message = f"{'; '.join(problems)}, so its edge cannot be restored"
        raise GraphCleanupValidationError(message)


def _obligation_edges(
    obligation_id: str, ids: tuple[str, ...], *, requires: bool
) -> list[EdgeRecord]:
    if requires:
        return [
            EdgeRecord(
                rel_type="REQUIRES",
                source_label="Obligation",
                source_id=obligation_id,
                target_label="Capability",
                target_id=capability_id,
            )
            for capability_id in ids
        ]
    return [
        EdgeRecord(
            rel_type="SATISFIED_BY",
            source_label="Requirement",
            source_id=requirement_id,
            target_label="Obligation",
            target_id=obligation_id,
        )
        for requirement_id in ids
    ]


def _obligation_digest(
    before: GraphSnapshot, inputs: ObligationUnmergeInputs, write: ObligationUnmergeWrite
) -> str:
    canonical = json.dumps(
        {
            "before": before.model_dump(mode="json"),
            "merge_approval_id": inputs.merge_approval_id,
            "write": write.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_obligation_unmerge(
    inputs: ObligationUnmergeInputs, state: ObligationUnmergeState
) -> ObligationUnmergePlan:
    """Plan recreating a deleted Obligation under its original id against the live `state`.

    The survivor's edges are never removed (a union cannot be attributed); those that could
    originate from the merge are listed beside those added since.

    Raises:
        GraphCleanupValidationError: the Obligation exists again, its marker is missing or
            re-pointed, the survivor, the Role, a Requirement or a Capability endpoint is gone
            (or a Capability is now a tombstone) -- each explained (AC-BI-020).
    """
    _check_obligation_state(inputs, state)
    absorbed_id, survivor_id = inputs.absorbed_id, inputs.survivor_id
    write = ObligationUnmergeWrite(
        role_id=inputs.role_id,
        satisfied_by_ids=inputs.satisfied_by_ids,
        requires_ids=inputs.requires_ids,
        properties=inputs.properties,
    )
    snapshot_keys = {(e.rel_type, e.source_id, e.target_id) for e in inputs.snapshot_survivor_edges}
    before_keys = {
        (e.rel_type, e.source_id, e.target_id) for e in inputs.survivor_edges_before_merge
    }
    current = [
        e
        for e in state.survivor_edges
        if (e.rel_type == "SATISFIED_BY" and e.target_id == survivor_id)
        or (e.rel_type == "REQUIRES" and e.source_id == survivor_id)
    ]
    added = _sorted_edges(
        [e for e in current if (e.rel_type, e.source_id, e.target_id) not in snapshot_keys]
    )
    absorbed_requirements = set(inputs.satisfied_by_ids)
    absorbed_capabilities = set(inputs.requires_ids)
    possibly = _sorted_edges(
        [
            e
            for e in current
            if (e.rel_type, e.source_id, e.target_id) in snapshot_keys
            and (e.rel_type, e.source_id, e.target_id) not in before_keys
            and (
                (e.rel_type == "SATISFIED_BY" and e.source_id in absorbed_requirements)
                or (e.rel_type == "REQUIRES" and e.target_id in absorbed_capabilities)
            )
        ]
    )
    restored = _sorted_edges(
        [
            EdgeRecord(
                rel_type="HAS",
                source_label="Role",
                source_id=inputs.role_id,
                target_label="Obligation",
                target_id=absorbed_id,
            ),
            *_obligation_edges(absorbed_id, inputs.satisfied_by_ids, requires=False),
            *_obligation_edges(absorbed_id, inputs.requires_ids, requires=True),
        ]
    )
    marker = NodeRecord(
        label="MergedObligation", id=absorbed_id, properties={"merged_into": survivor_id}
    )
    survivor_node = NodeRecord(
        label="Obligation", id=survivor_id, properties={"text": state.survivor_text}
    )
    restored_node = NodeRecord(
        label="Obligation", id=absorbed_id, properties=dict(inputs.properties)
    )
    before = GraphSnapshot(nodes=(marker, survivor_node), edges=_sorted_edges(current))
    after = GraphSnapshot(
        nodes=(restored_node, survivor_node), edges=_sorted_edges([*restored, *current])
    )
    digest = _obligation_digest(before, inputs, write)
    merged_text = inputs.properties.get("text")
    preview = ObligationUnmergePreview(
        merged_id=absorbed_id,
        merged_text=merged_text if isinstance(merged_text, str) else "",
        survivor_id=survivor_id,
        survivor_text=state.survivor_text,
        role_id=inputs.role_id,
        merge_approval_id=inputs.merge_approval_id,
        edges_to_restore=ObligationUnmergeEdgeSet(
            satisfied_by=inputs.satisfied_by_ids, requires=inputs.requires_ids
        ),
        survivor_added_edges=added,
        survivor_edges_possibly_from_merge=possibly,
        note=_OBLIGATION_NOTE,
        state_digest=digest,
    )
    return ObligationUnmergePlan(
        preview=preview,
        write=write,
        restored_edges=restored,
        before=before,
        after=after,
        state_digest=digest,
        inputs=inputs,
    )
