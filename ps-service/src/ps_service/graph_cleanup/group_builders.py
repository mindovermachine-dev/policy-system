"""Read-resolve-write for Graph Cleanup's graph edits, as gateway groups.

The gateway takes primitives, not Cypher, so each cleanup action is three steps: a fresh read of
the state (the `graph_reader` reads), a guard evaluated in Python that pins the state the
Compliance Officer previewed (`*_guard_holds`, pure), and the edit as primitives
(`build_*_group`, pure). A `write_*` function runs them in the order `graph_submission`
documents: the graph must be caught up, the log position P0 is read BEFORE the guard reads, a
failed guard raises `GraphCleanupStaleStateError` with nothing logged, and the group is submitted
with `ExpectedPosition(P0 + stamped)`, so a write that lands between the guard read and the
append is refused rather than missed.

One function set per cleanup action lives here; `executors` records the `applied` audit row first
and passes its id in as `audit_event_id`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_cleanup.errors import GraphCleanupStaleStateError
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_capability_unmerge_state,
    read_obligation_merge_state,
    read_obligation_unmerge_state,
    read_release_state,
)
from ps_service.graph_cleanup.graph_submission import (
    GraphWriteTarget,
    edge_identity,
    ensure_stamped,
    read_log_position,
    refuse_unstorable_values,
    require_caught_up,
    submit_cleanup_group,
)
from ps_service.graph_cleanup.merge_planner import EDGE_CLASSES
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    ExpectedPosition,
    MergeProperty,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_cleanup.models import (
        CapabilityUnmergePlan,
        CapabilityUnmergeState,
        CapabilityUnmergeWrite,
        ExpectedCounts,
        GoverningPolicy,
        MergeState,
        ObligationExpectedCounts,
        ObligationMergeState,
        ObligationUnmergePlan,
        ObligationUnmergeState,
        ObligationUnmergeWrite,
        ReleaseState,
    )
    from ps_service.graph_gateway.models import Primitive

__all__ = [
    "build_capability_merge_group",
    "build_capability_unmerge_group",
    "build_obligation_merge_group",
    "build_obligation_unmerge_group",
    "build_release_group",
    "capability_merge_guard_holds",
    "capability_unmerge_guard_holds",
    "obligation_merge_guard_holds",
    "obligation_unmerge_guard_holds",
    "release_guard_holds",
    "write_capability_merge",
    "write_capability_unmerge",
    "write_obligation_merge",
    "write_obligation_unmerge",
    "write_release",
]

_RELEASE_STALE_MESSAGE = (
    "the capability or its policy changed since the preview; nothing was written"
)


_CAPABILITY_MERGE_STALE_MESSAGE = "the capabilities changed since the preview; nothing was written"
_OBLIGATION_MERGE_STALE_MESSAGE = "the obligations changed since the preview; nothing was written"
_CAPABILITY_UNMERGE_STALE_MESSAGE = _CAPABILITY_MERGE_STALE_MESSAGE
_OBLIGATION_UNMERGE_STALE_MESSAGE = _OBLIGATION_MERGE_STALE_MESSAGE


def release_guard_holds(state: ReleaseState, *, policy_id: str) -> bool:
    """Whether the Capability is active and governed by the draft Policy `policy_id`.

    The reader reports a Capability with no `status` as `active`, so a node without a status
    counts as active here.

    Args:
        state: Fresh release state from `read_release_state`.
        policy_id: Id of the draft Policy the release was previewed against.

    Returns:
        True when the guard holds and the release may proceed; False when the state moved.
    """
    return (
        state.capability is not None
        and state.capability.status == "active"
        and any(p.id == policy_id and p.status == "draft" for p in state.policies)
    )


def build_release_group(*, capability_id: str, policy_id: str) -> tuple[Primitive, ...]:
    """The edit: delete the one `GOVERNED_BY` edge from the Capability to the Policy.

    The Capability and the Policy stay untouched. The edge is named by its identity,
    `GOVERNED_BY:{capability}->{policy}`; an edge without one is stamped ahead of the group.

    Args:
        capability_id: Id of the Capability being released.
        policy_id: Id of the Policy that governs it.

    Returns:
        A one-element tuple holding the `DeleteEdge` primitive.
    """
    return (
        DeleteEdge(
            type="GOVERNED_BY",
            identity=edge_identity("GOVERNED_BY", capability_id, policy_id),
            source=NodeRef(label="Capability", id=capability_id),
            target=NodeRef(label="Policy", id=policy_id),
        ),
    )


def write_release(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    capability_id: str,
    policy_id: str,
) -> None:
    """Delete the `GOVERNED_BY` edge from `capability_id` to the draft Policy `policy_id`.

    Args:
        graph: Handle used for the guard read.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        capability_id: Id of the Capability being released.
        policy_id: Id of the draft Policy that governs it.

    Side Effects:
        May log an edge-identity stamp group, then submits one group through the gateway.

    Raises:
        GraphCleanupStaleStateError: the guard did not hold (the edge is gone, the Capability is no
            longer active or the Policy is no longer a draft) or the log position moved; nothing of
            the release group was logged.
        GraphCleanupPersistenceError: the graph could not be read or the gateway failed.
    """
    require_caught_up(target)
    expected_position = read_log_position(target)
    state = read_release_state(graph, capability_id=capability_id)
    if not release_guard_holds(state, policy_id=policy_id):
        raise GraphCleanupStaleStateError(_RELEASE_STALE_MESSAGE)
    stamped = ensure_stamped(target, audit_event_id)
    submit_cleanup_group(
        target,
        build_release_group(capability_id=capability_id, policy_id=policy_id),
        audit_event_id=audit_event_id,
        stale_message=_RELEASE_STALE_MESSAGE,
        preconditions=(ExpectedPosition(position=expected_position + stamped),),
    )


# -- merge-capabilities -----------------------------------------------------------------------


def _distinct_policies(policies: tuple[GoverningPolicy, ...]) -> list[GoverningPolicy]:
    """The Policies once each, in read order."""
    return list({policy.id: policy for policy in policies}.values())


def _incoming_sources(state: MergeState, rel_type: str, target_id: str) -> list[str]:
    """The distinct sources of the `rel_type` edges into `target_id`, sorted."""
    return sorted(
        {e.source_id for e in state.edges if e.rel_type == rel_type and e.target_id == target_id}
    )


def capability_merge_guard_holds(
    state: MergeState, expected: ExpectedCounts, *, absorbed_id: str
) -> bool:
    """Whether a fresh read still matches the state the Compliance Officer previewed.

    Both Capabilities exist and are active (no `status` reads as active); the absorbed side has
    exactly the previewed number of `REQUIRES` / `COVERS` / `MITIGATED_BY` edges; each side is
    governed by exactly the previewed Policies in the previewed status; and the two sides are not
    governed by different Policies.

    Args:
        state: Fresh merge state from `read_capability_merge_state`.
        expected: Edge counts and Policy ids/statuses captured in the preview.
        absorbed_id: Id of the Capability being merged away.

    Returns:
        True when the guard holds and the merge may proceed; False when the state moved.
    """
    if (
        state.survivor is None
        or state.absorbed is None
        or state.survivor.status != "active"
        or state.absorbed.status != "active"
    ):
        return False
    counts = {
        rel_type: sum(
            1 for e in state.edges if e.rel_type == rel_type and e.target_id == absorbed_id
        )
        for rel_type, _ in EDGE_CLASSES
    }
    if (
        counts["REQUIRES"] != expected.requires
        or counts["COVERS"] != expected.covers
        or counts["MITIGATED_BY"] != expected.mitigated
    ):
        return False
    governing_absorbed = _distinct_policies(state.absorbed_policies)
    governing_survivor = _distinct_policies(state.survivor_policies)
    if (
        len(governing_absorbed) != expected.absorbed_governed
        or len(governing_survivor) != expected.survivor_governed
    ):
        return False
    if not all(
        p.id == expected.absorbed_policy_id and p.status == expected.absorbed_policy_status
        for p in governing_absorbed
    ) or not all(
        p.id == expected.survivor_policy_id and p.status == expected.survivor_policy_status
        for p in governing_survivor
    ):
        return False
    return not (
        len(governing_absorbed) == 1
        and len(governing_survivor) == 1
        and governing_absorbed[0].id != governing_survivor[0].id
    )


def build_capability_merge_group(
    state: MergeState, *, survivor_id: str, absorbed_id: str
) -> tuple[Primitive, ...]:
    """The edit: move the absorbed Capability's edges onto the survivor and tombstone it.

    In order: every incoming `REQUIRES` / `COVERS` / `MITIGATED_BY` edge is upserted onto the
    survivor (an endpoint already linked collapses into the existing edge, by identity); the
    absorbed side's `GOVERNED_BY` edge is upserted onto the survivor only when the survivor has no
    governing Policy; the moved edges and the absorbed `GOVERNED_BY` edge are deleted; the
    absorbed node becomes a `merged` tombstone with a `MERGED_INTO` edge to the survivor.

    Args:
        state: Merge state the edges and Policies are taken from.
        survivor_id: Id of the Capability that remains.
        absorbed_id: Id of the Capability that is tombstoned.

    Returns:
        The ordered primitives of the group.
    """
    survivor = NodeRef(label="Capability", id=survivor_id)
    absorbed = NodeRef(label="Capability", id=absorbed_id)
    moved = [
        (rel_type, source_label, source_id)
        for rel_type, source_label in EDGE_CLASSES
        for source_id in _incoming_sources(state, rel_type, absorbed_id)
    ]
    governing_absorbed = _distinct_policies(state.absorbed_policies)
    primitives: list[Primitive] = [
        UpsertEdge(
            type=rel_type,
            identity=edge_identity(rel_type, source_id, survivor_id),
            source=NodeRef(label=source_label, id=source_id),
            target=survivor,
        )
        for rel_type, source_label, source_id in moved
    ]
    if not state.survivor_policies:
        primitives.extend(
            UpsertEdge(
                type="GOVERNED_BY",
                identity=edge_identity("GOVERNED_BY", survivor_id, policy.id),
                source=survivor,
                target=NodeRef(label="Policy", id=policy.id),
            )
            for policy in governing_absorbed
        )
    primitives.extend(
        DeleteEdge(
            type=rel_type,
            identity=edge_identity(rel_type, source_id, absorbed_id),
            source=NodeRef(label=source_label, id=source_id),
            target=absorbed,
        )
        for rel_type, source_label, source_id in moved
    )
    primitives.extend(
        DeleteEdge(
            type="GOVERNED_BY",
            identity=edge_identity("GOVERNED_BY", absorbed_id, policy.id),
            source=absorbed,
            target=NodeRef(label="Policy", id=policy.id),
        )
        for policy in governing_absorbed
    )
    primitives.append(
        MergeProperty(label="Capability", id=absorbed_id, properties={"status": "merged"})
    )
    primitives.append(
        UpsertEdge(
            type="MERGED_INTO",
            identity=edge_identity("MERGED_INTO", absorbed_id, survivor_id),
            source=absorbed,
            target=survivor,
        )
    )
    return tuple(primitives)


def write_capability_merge(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    survivor_id: str,
    absorbed_id: str,
    expected: ExpectedCounts,
) -> None:
    """Tombstone `absorbed_id` into `survivor_id` as one gateway group.

    Args:
        graph: Handle used for the guard read.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        survivor_id: Id of the Capability that remains.
        absorbed_id: Id of the Capability that is tombstoned.
        expected: Edge counts and Policy ids/statuses captured in the preview.

    Side Effects:
        May log an edge-identity stamp group, then submits one group through the gateway.

    Raises:
        GraphCleanupStaleStateError: the guard did not hold (a node, its status, an edge count or
            its governing Policy differs from `expected`) or the log position moved; nothing of
            the merge group was logged.
        GraphCleanupPersistenceError: the graph could not be read or the gateway failed.
    """
    require_caught_up(target)
    expected_position = read_log_position(target)
    state = read_capability_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    if not capability_merge_guard_holds(state, expected, absorbed_id=absorbed_id):
        raise GraphCleanupStaleStateError(_CAPABILITY_MERGE_STALE_MESSAGE)
    with refuse_unstorable_values():
        primitives = build_capability_merge_group(
            state, survivor_id=survivor_id, absorbed_id=absorbed_id
        )
    stamped = ensure_stamped(target, audit_event_id)
    submit_cleanup_group(
        target,
        primitives,
        audit_event_id=audit_event_id,
        stale_message=_CAPABILITY_MERGE_STALE_MESSAGE,
        preconditions=(ExpectedPosition(position=expected_position + stamped),),
    )


# -- merge-obligations ------------------------------------------------------------------------


def obligation_merge_guard_holds(
    state: ObligationMergeState,
    expected: ObligationExpectedCounts,
    *,
    survivor_id: str,
    absorbed_id: str,
) -> bool:
    """Whether two distinct Obligations still share a Role and have the previewed edge counts.

    Args:
        state: Fresh merge state from `read_obligation_merge_state`.
        expected: Counts of the absorbed side's `SATISFIED_BY` and `REQUIRES` edges previewed.
        survivor_id: Id of the Obligation that remains.
        absorbed_id: Id of the Obligation that is deleted.

    Returns:
        True when the guard holds and the merge may proceed; False when the state moved.
    """
    if state.survivor is None or state.absorbed is None or survivor_id == absorbed_id:
        return False
    if not {r.id for r in state.survivor_roles} & {r.id for r in state.absorbed_roles}:
        return False
    satisfied = sum(
        1 for e in state.edges if e.rel_type == "SATISFIED_BY" and e.target_id == absorbed_id
    )
    requires = sum(
        1 for e in state.edges if e.rel_type == "REQUIRES" and e.source_id == absorbed_id
    )
    return satisfied == expected.satisfied and requires == expected.requires


def build_obligation_merge_group(
    state: ObligationMergeState, *, survivor_id: str, absorbed_id: str
) -> tuple[Primitive, ...]:
    """The edit: union the absorbed Obligation's edges onto the survivor, mark it, delete it.

    The `MergedObligation` marker is upserted before the `DeleteNode` and in the same group, so a
    delete without a redirect cannot be logged. `DeleteNode` removes every edge of the absorbed
    Obligation, its `HAS` edge included.

    Args:
        state: Merge state the edges are taken from.
        survivor_id: Id of the Obligation that remains.
        absorbed_id: Id of the Obligation that is deleted.

    Returns:
        The ordered primitives of the group.
    """
    survivor = NodeRef(label="Obligation", id=survivor_id)
    satisfied_by = sorted(
        {
            e.source_id
            for e in state.edges
            if e.rel_type == "SATISFIED_BY" and e.target_id == absorbed_id
        }
    )
    requires = sorted(
        {
            e.target_id
            for e in state.edges
            if e.rel_type == "REQUIRES" and e.source_id == absorbed_id
        }
    )
    primitives: list[Primitive] = [
        UpsertEdge(
            type="SATISFIED_BY",
            identity=edge_identity("SATISFIED_BY", requirement_id, survivor_id),
            source=NodeRef(label="Requirement", id=requirement_id),
            target=survivor,
        )
        for requirement_id in satisfied_by
    ]
    primitives.extend(
        UpsertEdge(
            type="REQUIRES",
            identity=edge_identity("REQUIRES", survivor_id, capability_id),
            source=survivor,
            target=NodeRef(label="Capability", id=capability_id),
        )
        for capability_id in requires
    )
    primitives.append(
        UpsertNode(
            label="MergedObligation", id=absorbed_id, properties={"merged_into": survivor_id}
        )
    )
    primitives.append(DeleteNode(label="Obligation", id=absorbed_id))
    return tuple(primitives)


def write_obligation_merge(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    survivor_id: str,
    absorbed_id: str,
    expected: ObligationExpectedCounts,
) -> None:
    """Union `absorbed_id`'s edges onto `survivor_id` and delete it, as one gateway group.

    Args:
        graph: Handle used for the guard read.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        survivor_id: Id of the Obligation that remains.
        absorbed_id: Id of the Obligation that is deleted.
        expected: Edge counts captured in the preview.

    Side Effects:
        May log an edge-identity stamp group, then submits one group through the gateway.

    Raises:
        GraphCleanupStaleStateError: the guard did not hold (a node is gone, the pair no longer
            shares a Role, or an edge count differs from `expected`) or the log position moved;
            nothing of the merge group was logged.
        GraphCleanupPersistenceError: the graph could not be read or the gateway failed.
    """
    require_caught_up(target)
    expected_position = read_log_position(target)
    state = read_obligation_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    if not obligation_merge_guard_holds(
        state, expected, survivor_id=survivor_id, absorbed_id=absorbed_id
    ):
        raise GraphCleanupStaleStateError(_OBLIGATION_MERGE_STALE_MESSAGE)
    with refuse_unstorable_values():
        primitives = build_obligation_merge_group(
            state, survivor_id=survivor_id, absorbed_id=absorbed_id
        )
    stamped = ensure_stamped(target, audit_event_id)
    submit_cleanup_group(
        target,
        primitives,
        audit_event_id=audit_event_id,
        stale_message=_OBLIGATION_MERGE_STALE_MESSAGE,
        preconditions=(ExpectedPosition(position=expected_position + stamped),),
    )


# -- unmerge-capability -----------------------------------------------------------------------


def _unmerge_ids(
    write: CapabilityUnmergeWrite,
) -> tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...]:
    """`(edge type, endpoint label, restore ids, remove ids)` per edge class, in class order."""
    by_type = {
        "REQUIRES": (write.restore_requires_ids, write.remove_requires_ids),
        "COVERS": (write.restore_covers_ids, write.remove_covers_ids),
        "MITIGATED_BY": (write.restore_mitigated_ids, write.remove_mitigated_ids),
    }
    return tuple((rel_type, label, *by_type[rel_type]) for rel_type, label in EDGE_CLASSES)


def capability_unmerge_guard_holds(
    state: CapabilityUnmergeState,
    write: CapabilityUnmergeWrite,
    *,
    absorbed_id: str,
    survivor_id: str,
) -> bool:
    """Whether a fresh read still matches the state the unmerge was planned against.

    The absorbed node is a `merged` tombstone with a `MERGED_INTO` edge to the survivor, the
    survivor is active, every endpoint to restore exists, every edge to remove is still on the
    survivor, the Policy to restore exists and, when the merge moved the policy edge, the survivor
    holds exactly one `GOVERNED_BY` edge to it.

    Args:
        state: Fresh unmerge state from `read_capability_unmerge_state`.
        write: The planned restore and remove parameters.
        absorbed_id: Id of the tombstoned Capability.
        survivor_id: Id of the Capability it was merged into.

    Returns:
        True when the guard holds and the unmerge may proceed; False when the state moved.
    """
    if (
        state.absorbed is None
        or state.survivor is None
        or state.absorbed.status != "merged"
        or survivor_id not in state.redirects.get(absorbed_id, ())
        or state.survivor.status != "active"
    ):
        return False
    current = {
        (e.rel_type, e.source_id) for e in state.survivor_edges if e.target_id == survivor_id
    }
    for rel_type, _label, restore_ids, remove_ids in _unmerge_ids(write):
        if not set(restore_ids) <= set(state.existing_endpoints.get(rel_type, ())):
            return False
        if any((rel_type, source_id) not in current for source_id in remove_ids):
            return False
    if write.restore_policy_id is not None and not state.policy_exists:
        return False
    return not (
        write.remove_policy_edge
        and state.survivor_policy_ids.count(write.restore_policy_id or "") != 1
    )


def build_capability_unmerge_group(
    write: CapabilityUnmergeWrite, *, absorbed_id: str, survivor_id: str
) -> tuple[Primitive, ...]:
    """The edit: put the moved edges back on the absorbed Capability and un-tombstone it.

    In order: every planned edge is upserted back onto the absorbed node; ONLY the planned edges
    are deleted from the survivor (an edge it gained since the merge is never named); the
    `GOVERNED_BY` edge to the restored Policy is upserted on the absorbed node and deleted from
    the survivor when the merge had moved it; the node becomes `active` and its `MERGED_INTO` edge
    is deleted.

    Args:
        write: The planned restore and remove parameters.
        absorbed_id: Id of the tombstoned Capability to restore.
        survivor_id: Id of the Capability it was merged into.

    Returns:
        The ordered primitives of the group.
    """
    survivor = NodeRef(label="Capability", id=survivor_id)
    absorbed = NodeRef(label="Capability", id=absorbed_id)
    classes = _unmerge_ids(write)
    primitives: list[Primitive] = [
        UpsertEdge(
            type=rel_type,
            identity=edge_identity(rel_type, source_id, absorbed_id),
            source=NodeRef(label=label, id=source_id),
            target=absorbed,
        )
        for rel_type, label, restore_ids, _ in classes
        for source_id in restore_ids
    ]
    primitives.extend(
        DeleteEdge(
            type=rel_type,
            identity=edge_identity(rel_type, source_id, survivor_id),
            source=NodeRef(label=label, id=source_id),
            target=survivor,
        )
        for rel_type, label, _, remove_ids in classes
        for source_id in remove_ids
    )
    if write.restore_policy_id is not None:
        policy = NodeRef(label="Policy", id=write.restore_policy_id)
        primitives.append(
            UpsertEdge(
                type="GOVERNED_BY",
                identity=edge_identity("GOVERNED_BY", absorbed_id, write.restore_policy_id),
                source=absorbed,
                target=policy,
            )
        )
        if write.remove_policy_edge:
            primitives.append(
                DeleteEdge(
                    type="GOVERNED_BY",
                    identity=edge_identity("GOVERNED_BY", survivor_id, write.restore_policy_id),
                    source=survivor,
                    target=policy,
                )
            )
    primitives.append(
        MergeProperty(label="Capability", id=absorbed_id, properties={"status": "active"})
    )
    primitives.append(
        DeleteEdge(
            type="MERGED_INTO",
            identity=edge_identity("MERGED_INTO", absorbed_id, survivor_id),
            source=absorbed,
            target=survivor,
        )
    )
    return tuple(primitives)


def write_capability_unmerge(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    plan: CapabilityUnmergePlan,
) -> None:
    """Reverse one capability merge as one gateway group.

    Args:
        graph: Handle used for the guard read.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        plan: The approved unmerge plan: ids, inputs and write parameters.

    Side Effects:
        May log an edge-identity stamp group, then submits one group through the gateway.

    Raises:
        GraphCleanupStaleStateError: the guard did not hold (the tombstone or its redirect, the
            survivor's status, an endpoint, an edge or the Policy differs from the plan) or the
            log position moved; nothing of the unmerge group was logged.
        GraphCleanupPersistenceError: the graph could not be read or the gateway failed.
    """
    absorbed_id, survivor_id = plan.preview.merged_id, plan.preview.survivor_id
    require_caught_up(target)
    expected_position = read_log_position(target)
    state = read_capability_unmerge_state(graph, inputs=plan.inputs)
    if not capability_unmerge_guard_holds(
        state, plan.write, absorbed_id=absorbed_id, survivor_id=survivor_id
    ):
        raise GraphCleanupStaleStateError(_CAPABILITY_UNMERGE_STALE_MESSAGE)
    with refuse_unstorable_values():
        primitives = build_capability_unmerge_group(
            plan.write, absorbed_id=absorbed_id, survivor_id=survivor_id
        )
    stamped = ensure_stamped(target, audit_event_id)
    submit_cleanup_group(
        target,
        primitives,
        audit_event_id=audit_event_id,
        stale_message=_CAPABILITY_UNMERGE_STALE_MESSAGE,
        preconditions=(ExpectedPosition(position=expected_position + stamped),),
    )


# -- unmerge-obligation -----------------------------------------------------------------------


def obligation_unmerge_guard_holds(
    state: ObligationUnmergeState,
    write: ObligationUnmergeWrite,
    *,
    absorbed_id: str,
    survivor_id: str,
) -> bool:
    """Whether the marker, the Obligation's absence and every endpoint to link still hold.

    The `MergedObligation` marker points at the survivor, the Obligation does not exist again, the
    Role exists, every Requirement exists and every Capability exists and is not a tombstone.

    Args:
        state: Fresh unmerge state from `read_obligation_unmerge_state`.
        write: The planned Role, Requirement and Capability ids to reattach.
        absorbed_id: Id of the deleted Obligation.
        survivor_id: Id of the Obligation it was merged into.

    Returns:
        True when the guard holds and the unmerge may proceed; False when the state moved.
    """
    return (
        survivor_id in state.marker_targets.get(absorbed_id, ())
        and not state.absorbed_exists
        and state.role_exists
        and set(write.satisfied_by_ids) <= set(state.existing_requirements)
        and set(write.requires_ids) <= set(state.capability_statuses)
        and all(state.capability_statuses[c] != "merged" for c in write.requires_ids)
    )


def build_obligation_unmerge_group(
    write: ObligationUnmergeWrite, *, absorbed_id: str
) -> tuple[Primitive, ...]:
    """The edit: recreate the Obligation by id with its edges and delete the marker.

    The recreate is an upsert by id, so replaying the logged group (or a retry of a half-applied
    append) finds the node and its edges already there instead of adding a second copy.
    The marker is deleted last, in the same group, so a recreate without the marker removal cannot
    be logged. The survivor's edges are never named.

    Args:
        write: The planned Role, Requirement and Capability ids and node properties.
        absorbed_id: Id of the Obligation to recreate.

    Returns:
        The ordered primitives of the group.
    """
    absorbed = NodeRef(label="Obligation", id=absorbed_id)
    primitives: list[Primitive] = [
        UpsertNode(label="Obligation", id=absorbed_id, properties=dict(write.properties)),
        UpsertEdge(
            type="HAS",
            identity=edge_identity("HAS", write.role_id, absorbed_id),
            source=NodeRef(label="Role", id=write.role_id),
            target=absorbed,
        ),
    ]
    primitives.extend(
        UpsertEdge(
            type="SATISFIED_BY",
            identity=edge_identity("SATISFIED_BY", requirement_id, absorbed_id),
            source=NodeRef(label="Requirement", id=requirement_id),
            target=absorbed,
        )
        for requirement_id in write.satisfied_by_ids
    )
    primitives.extend(
        UpsertEdge(
            type="REQUIRES",
            identity=edge_identity("REQUIRES", absorbed_id, capability_id),
            source=absorbed,
            target=NodeRef(label="Capability", id=capability_id),
        )
        for capability_id in write.requires_ids
    )
    primitives.append(DeleteNode(label="MergedObligation", id=absorbed_id))
    return tuple(primitives)


def write_obligation_unmerge(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    plan: ObligationUnmergePlan,
) -> None:
    """Recreate the deleted Obligation and drop its marker, as one gateway group.

    Args:
        graph: Handle used for the guard read.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        plan: The approved unmerge plan: ids, inputs and write parameters.

    Side Effects:
        May log an edge-identity stamp group, then submits one group through the gateway.

    Raises:
        GraphCleanupStaleStateError: the guard did not hold (the marker is gone or re-pointed, the
            Obligation exists again, the Role, a Requirement or a Capability is gone or a
            Capability is a tombstone) or the log position moved; nothing of the unmerge group
            was logged.
        GraphCleanupPersistenceError: the graph could not be read or the gateway failed.
    """
    absorbed_id, survivor_id = plan.preview.merged_id, plan.preview.survivor_id
    require_caught_up(target)
    expected_position = read_log_position(target)
    state = read_obligation_unmerge_state(graph, inputs=plan.inputs)
    if not obligation_unmerge_guard_holds(
        state, plan.write, absorbed_id=absorbed_id, survivor_id=survivor_id
    ):
        raise GraphCleanupStaleStateError(_OBLIGATION_UNMERGE_STALE_MESSAGE)
    with refuse_unstorable_values():
        primitives = build_obligation_unmerge_group(plan.write, absorbed_id=absorbed_id)
    stamped = ensure_stamped(target, audit_event_id)
    submit_cleanup_group(
        target,
        primitives,
        audit_event_id=audit_event_id,
        stale_message=_OBLIGATION_UNMERGE_STALE_MESSAGE,
        preconditions=(ExpectedPosition(position=expected_position + stamped),),
    )
