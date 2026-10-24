"""Read-resolve-write for Graph Cleanup's graph edits, as gateway groups.

The gateway takes primitives, not Cypher, so each cleanup action is three steps: a fresh read of
the state (the `graph_reader` reads), a guard evaluated in Python that pins the state the
Compliance Officer previewed (`*_guard_holds`, pure), and the edit as primitives
(`build_*_group`, pure). A `write_*` function runs them in the order `graph_submission`
documents: the graph must be caught up, the log position P0 is read BEFORE the guard reads, a
failed guard raises `GraphCleanupStaleStateError` with nothing logged, and the group is submitted
with `ExpectedPosition(P0 + stamped)`, so a write that lands between the guard read and the
append is refused rather than missed.

The split guard re-reads the baselines too and compares the previewed state digest, because the
split's edge rule depends on what the baselines hold. One function set per cleanup action lives
here; `executors` records the `applied` audit row first
and passes its id in as `audit_event_id`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_cleanup.errors import (
    GraphCleanupStaleStateError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_capability_unmerge_state,
    read_obligation_merge_state,
    read_obligation_unmerge_state,
    read_release_state,
    read_role_merge_state,
    read_role_unmerge_state,
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
from ps_service.graph_cleanup.split_planner import plan_capability_split
from ps_service.graph_cleanup.split_reader import read_capability_split_state
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    ExpectedPosition,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_cleanup.models import (
        CapabilitySplitPlan,
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
        RoleExpectedCounts,
        RoleMergeState,
        RoleUnmergePlan,
        RoleUnmergeState,
        RoleUnmergeWrite,
        SplitEdgeSet,
    )
    from ps_service.graph_cleanup.split_planner import CapabilitySplitState
    from ps_service.graph_gateway.models import Primitive

__all__ = [
    "build_capability_merge_group",
    "build_capability_split_group",
    "build_capability_unmerge_group",
    "build_obligation_merge_group",
    "build_obligation_unmerge_group",
    "build_release_group",
    "build_role_merge_group",
    "build_role_unmerge_group",
    "capability_merge_guard_holds",
    "capability_split_guard_holds",
    "capability_unmerge_guard_holds",
    "obligation_merge_guard_holds",
    "obligation_unmerge_guard_holds",
    "release_guard_holds",
    "role_merge_guard_holds",
    "role_unmerge_guard_holds",
    "write_capability_merge",
    "write_capability_split",
    "write_capability_unmerge",
    "write_obligation_merge",
    "write_obligation_unmerge",
    "write_release",
    "write_role_merge",
    "write_role_unmerge",
]

_RELEASE_STALE_MESSAGE = (
    "the capability or its policy changed since the preview; nothing was written"
)


_CAPABILITY_MERGE_STALE_MESSAGE = "the capabilities changed since the preview; nothing was written"
_OBLIGATION_MERGE_STALE_MESSAGE = "the obligations changed since the preview; nothing was written"
_ROLE_MERGE_STALE_MESSAGE = "the roles changed since the preview; nothing was written"
_CAPABILITY_UNMERGE_STALE_MESSAGE = _CAPABILITY_MERGE_STALE_MESSAGE
_OBLIGATION_UNMERGE_STALE_MESSAGE = _OBLIGATION_MERGE_STALE_MESSAGE
_ROLE_UNMERGE_STALE_MESSAGE = _ROLE_MERGE_STALE_MESSAGE
_CAPABILITY_SPLIT_STALE_MESSAGE = (
    "the resolved capability or its terminal changed since the preview; nothing was written"
)


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
    state: MergeState, *, survivor_id: str, absorbed_id: str, review_id: str | None = None
) -> tuple[Primitive, ...]:
    """The edit: move the absorbed Capability's edges onto the survivor, mark it, delete it.

    In order: every incoming `REQUIRES` / `COVERS` / `MITIGATED_BY` edge is upserted onto the
    survivor (an endpoint already linked collapses into the existing edge, by identity); the
    absorbed side's `GOVERNED_BY` edge is upserted onto the survivor only when the survivor has no
    governing Policy; the `MergedCapability` marker is upserted; the absorbed node is deleted.
    The marker comes before the `DeleteNode` in the same group, so a delete without a redirect
    cannot be logged. `DeleteNode` removes every remaining edge of the absorbed Capability, so no
    per-edge delete is named. When `review_id` is given, the near-miss `PendingReview` it names
    is deleted last, in the same group.

    Args:
        state: Merge state the edges and Policies are taken from.
        survivor_id: Id of the Capability that remains.
        absorbed_id: Id of the Capability that is deleted.
        review_id: Id of the `PendingReview` this merge resolves; None when there is none.

    Returns:
        The ordered primitives of the group.
    """
    survivor = NodeRef(label="Capability", id=survivor_id)
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
    primitives.append(
        UpsertNode(
            label="MergedCapability", id=absorbed_id, properties={"merged_into": survivor_id}
        )
    )
    primitives.append(DeleteNode(label="Capability", id=absorbed_id))
    if review_id is not None:
        primitives.append(DeleteNode(label="PendingReview", id=review_id))
    return tuple(primitives)


def write_capability_merge(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    survivor_id: str,
    absorbed_id: str,
    expected: ExpectedCounts,
    review_id: str | None = None,
) -> None:
    """Merge `absorbed_id` into `survivor_id`, leaving a marker, as one gateway group.

    Args:
        graph: Handle used for the guard read.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        survivor_id: Id of the Capability that remains.
        absorbed_id: Id of the Capability that is deleted.
        expected: Edge counts and Policy ids/statuses captured in the preview.
        review_id: Id of the near-miss `PendingReview` deleted in the same group; None when none.

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
            state, survivor_id=survivor_id, absorbed_id=absorbed_id, review_id=review_id
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


# -- merge-roles ------------------------------------------------------------------------------


def role_merge_guard_holds(
    state: RoleMergeState,
    expected: RoleExpectedCounts,
    *,
    survivor_id: str,
    absorbed_id: str,
) -> bool:
    """Whether two distinct Roles still share one instrument and have the previewed `HAS` sets.

    Args:
        state: Fresh merge state from `read_role_merge_state`.
        expected: The `HAS` sets of both Roles as previewed.
        survivor_id: Id of the Role that remains.
        absorbed_id: Id of the Role that is deleted.

    Returns:
        True when the guard holds and the merge may proceed; False when the state moved.
    """
    if state.survivor is None or state.absorbed is None or survivor_id == absorbed_id:
        return False
    instruments = {d.instrument_id for d in (*state.survivor_defines, *state.absorbed_defines)}
    return (
        len(state.survivor_defines) == 1
        and len(state.absorbed_defines) == 1
        and len(instruments) == 1
        and not state.foreign_bearers
        and state.absorbed_obligation_ids == expected.absorbed_obligation_ids
        and state.survivor_obligation_ids == expected.survivor_obligation_ids
    )


def build_role_merge_group(
    state: RoleMergeState, *, survivor_id: str, absorbed_id: str, review_id: str | None = None
) -> tuple[Primitive, ...]:
    """The edit: move the absorbed Role's Obligations to the survivor, mark it, delete it.

    Each `HAS` edge is upserted from the survivor under the standard edge identity, so a later
    `HAS` upsert from a re-ingest collapses onto it. The `MergedRole` marker is upserted before
    the `DeleteNode` and in the same group, so a delete without a redirect cannot be logged.
    `DeleteNode` removes every edge of the absorbed Role, its `DEFINES` and old `HAS` edges
    included; the survivor's `DEFINES` edge is never touched. When `review_id` is given, the
    near-miss `PendingReview` it names is deleted last, in the same group.

    Args:
        state: Merge state the Obligations are taken from.
        survivor_id: Id of the Role that remains.
        absorbed_id: Id of the Role that is deleted.
        review_id: Id of the `PendingReview` this merge resolves; None when there is none.

    Returns:
        The ordered primitives of the group.
    """
    survivor = NodeRef(label="Role", id=survivor_id)
    primitives: list[Primitive] = [
        UpsertEdge(
            type="HAS",
            identity=edge_identity("HAS", survivor_id, obligation_id),
            source=survivor,
            target=NodeRef(label="Obligation", id=obligation_id),
        )
        for obligation_id in sorted(state.absorbed_obligation_ids)
    ]
    primitives.append(
        UpsertNode(label="MergedRole", id=absorbed_id, properties={"merged_into": survivor_id})
    )
    primitives.append(DeleteNode(label="Role", id=absorbed_id))
    if review_id is not None:
        primitives.append(DeleteNode(label="PendingReview", id=review_id))
    return tuple(primitives)


def write_role_merge(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    survivor_id: str,
    absorbed_id: str,
    expected: RoleExpectedCounts,
    review_id: str | None = None,
) -> None:
    """Move `absorbed_id`'s Obligations to `survivor_id` and delete it, as one gateway group.

    Args:
        graph: Handle used for the guard read.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        survivor_id: Id of the Role that remains.
        absorbed_id: Id of the Role that is deleted.
        expected: The `HAS` sets captured in the preview.
        review_id: Id of the near-miss `PendingReview` deleted in the same group; None when none.

    Side Effects:
        May log an edge-identity stamp group, then submits one group through the gateway.

    Raises:
        GraphCleanupStaleStateError: the guard did not hold (a Role is gone, the pair no longer
            shares one instrument, or a `HAS` set differs from `expected`) or the log position
            moved; nothing of the merge group was logged.
        GraphCleanupPersistenceError: the graph could not be read or the gateway failed.
    """
    require_caught_up(target)
    expected_position = read_log_position(target)
    state = read_role_merge_state(graph, survivor_id=survivor_id, absorbed_id=absorbed_id)
    if not role_merge_guard_holds(
        state, expected, survivor_id=survivor_id, absorbed_id=absorbed_id
    ):
        raise GraphCleanupStaleStateError(_ROLE_MERGE_STALE_MESSAGE)
    with refuse_unstorable_values():
        primitives = build_role_merge_group(
            state, survivor_id=survivor_id, absorbed_id=absorbed_id, review_id=review_id
        )
    stamped = ensure_stamped(target, audit_event_id)
    submit_cleanup_group(
        target,
        primitives,
        audit_event_id=audit_event_id,
        stale_message=_ROLE_MERGE_STALE_MESSAGE,
        preconditions=(ExpectedPosition(position=expected_position + stamped),),
    )


# -- unmerge-roles ----------------------------------------------------------------------------


def role_unmerge_guard_holds(
    state: RoleUnmergeState, write: RoleUnmergeWrite, *, absorbed_id: str, survivor_id: str
) -> bool:
    """Whether the marker, the Role's absence and every moved Obligation's bearer still hold.

    The `MergedRole` marker points at the survivor, no Role has the absorbed id, the survivor and
    the instrument exist, and every moved Obligation exists and is borne by the survivor alone.

    Args:
        state: Fresh unmerge state from `read_role_unmerge_state`.
        write: The planned instrument and Obligations to move back.
        absorbed_id: Id of the deleted Role.
        survivor_id: Id of the Role it was merged into.

    Returns:
        True when the guard holds and the unmerge may proceed; False when the state moved.
    """
    return (
        survivor_id in state.marker_targets.get(absorbed_id, ())
        and not state.absorbed_exists
        and state.survivor is not None
        and state.instrument_exists
        and all(state.moved_bearers.get(o) == (survivor_id,) for o in write.moved_obligation_ids)
    )


def build_role_unmerge_group(
    write: RoleUnmergeWrite, *, absorbed_id: str, survivor_id: str
) -> tuple[Primitive, ...]:
    """The edit: recreate the Role with its edges, move its Obligations back, drop the marker.

    Every upsert is by id or edge identity, so replaying the logged group finds the Role and its
    edges already there. The `DEFINES` edge and each `HAS` edge use the standard edge identity, so
    a later re-ingest upserts onto them. A moved Obligation's `HAS` edge from the survivor is
    deleted after the one from the recreated Role is upserted, so it is never without a bearer.
    The marker is deleted last, in the same group, so a recreate without the marker removal cannot
    be logged. Obligations the survivor gained since the merge are never named.

    Args:
        write: The planned Role properties, instrument and moved Obligations.
        absorbed_id: Id of the Role to recreate.
        survivor_id: Id of the Role the Obligations move back from.

    Returns:
        The ordered primitives of the group.
    """
    absorbed = NodeRef(label="Role", id=absorbed_id)
    survivor = NodeRef(label="Role", id=survivor_id)
    obligations = [NodeRef(label="Obligation", id=o) for o in write.moved_obligation_ids]
    primitives: list[Primitive] = [
        UpsertNode(label="Role", id=absorbed_id, properties=dict(write.absorbed_properties)),
        UpsertEdge(
            type="DEFINES",
            identity=edge_identity("DEFINES", write.instrument_id, absorbed_id),
            source=NodeRef(label="RegulatoryInstrument", id=write.instrument_id),
            target=absorbed,
            properties=(
                {"source_ref": write.defines_source_ref}
                if write.defines_source_ref is not None
                else {}
            ),
        ),
    ]
    primitives.extend(
        UpsertEdge(
            type="HAS",
            identity=edge_identity("HAS", absorbed_id, obligation.id),
            source=absorbed,
            target=obligation,
        )
        for obligation in obligations
    )
    primitives.extend(
        DeleteEdge(
            type="HAS",
            identity=edge_identity("HAS", survivor_id, obligation.id),
            source=survivor,
            target=obligation,
        )
        for obligation in obligations
    )
    primitives.append(DeleteNode(label="MergedRole", id=absorbed_id))
    return tuple(primitives)


def write_role_unmerge(
    graph: GraphHandle,
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    plan: RoleUnmergePlan,
) -> None:
    """Recreate the merged-away Role, move its Obligations back and drop its marker, as one group.

    Args:
        graph: Handle used for the guard read.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        plan: The approved unmerge plan: ids, inputs and write parameters.

    Side Effects:
        May log an edge-identity stamp group, then submits one group through the gateway.

    Raises:
        GraphCleanupStaleStateError: the guard did not hold (the marker is gone or re-pointed, a
            Role has the absorbed id again, the survivor or instrument is gone, or a moved
            Obligation is gone or borne by another Role) or the log position moved; nothing of the
            unmerge group was logged.
        GraphCleanupPersistenceError: the graph could not be read or the gateway failed.
    """
    absorbed_id, survivor_id = plan.preview.merged_id, plan.preview.survivor_id
    require_caught_up(target)
    expected_position = read_log_position(target)
    state = read_role_unmerge_state(graph, inputs=plan.inputs)
    if not role_unmerge_guard_holds(
        state, plan.write, absorbed_id=absorbed_id, survivor_id=survivor_id
    ):
        raise GraphCleanupStaleStateError(_ROLE_UNMERGE_STALE_MESSAGE)
    with refuse_unstorable_values():
        primitives = build_role_unmerge_group(
            plan.write, absorbed_id=absorbed_id, survivor_id=survivor_id
        )
    stamped = ensure_stamped(target, audit_event_id)
    submit_cleanup_group(
        target,
        primitives,
        audit_event_id=audit_event_id,
        stale_message=_ROLE_UNMERGE_STALE_MESSAGE,
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

    The absorbed Capability does not exist and its `MergedCapability` marker names the survivor,
    the survivor is active, every endpoint to restore exists, every edge to remove is still on the
    survivor, the Policy to restore exists and, when the merge moved the policy edge, the survivor
    holds exactly one `GOVERNED_BY` edge to it.

    Args:
        state: Fresh unmerge state from `read_capability_unmerge_state`.
        write: The planned restore and remove parameters.
        absorbed_id: Id of the deleted Capability.
        survivor_id: Id of the Capability it was merged into.

    Returns:
        True when the guard holds and the unmerge may proceed; False when the state moved.
    """
    if (
        state.absorbed_status is not None
        or state.survivor is None
        or survivor_id not in state.marker_targets.get(absorbed_id, ())
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
    """The edit: recreate the absorbed Capability with its edges and delete the marker.

    In order: the node is upserted by id with its original properties (so replaying the logged
    group, or a retry of a half-applied append, finds it already there instead of adding a
    second copy); every planned edge is upserted back onto it; ONLY the planned edges are deleted
    from the survivor (an edge it gained since the merge is never named); the `GOVERNED_BY` edge
    to the restored Policy is upserted on it and deleted from the survivor when the merge had
    moved it; the `MergedCapability` marker is deleted last, in the same group, so a recreate
    without the marker removal cannot be logged.

    Args:
        write: The planned restore and remove parameters and the node's properties.
        absorbed_id: Id of the deleted Capability to recreate.
        survivor_id: Id of the Capability it was merged into.

    Returns:
        The ordered primitives of the group.
    """
    survivor = NodeRef(label="Capability", id=survivor_id)
    absorbed = NodeRef(label="Capability", id=absorbed_id)
    classes = _unmerge_ids(write)
    primitives: list[Primitive] = [
        UpsertNode(label="Capability", id=absorbed_id, properties=dict(write.properties))
    ]
    primitives.extend(
        UpsertEdge(
            type=rel_type,
            identity=edge_identity(rel_type, source_id, absorbed_id),
            source=NodeRef(label=label, id=source_id),
            target=absorbed,
        )
        for rel_type, label, restore_ids, _ in classes
        for source_id in restore_ids
    )
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
    primitives.append(DeleteNode(label="MergedCapability", id=absorbed_id))
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
        GraphCleanupStaleStateError: the guard did not hold (the marker, the absorbed id existing
            again, the survivor's status, an endpoint, an edge or the Policy differs from the
            plan) or the log position moved; nothing of the unmerge group was logged.
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
    Role exists, every Requirement exists and every Capability exists.

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
        and set(write.requires_ids) <= set(state.existing_capabilities)
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
            Capability has been merged away) or the log position moved; nothing of the unmerge group
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


# -- split-capability ------------------------------------------------------------------------


def capability_split_guard_holds(state: CapabilitySplitState, *, state_digest: str) -> bool:
    """Whether a fresh read still matches the split state the Compliance Officer previewed.

    The state digest covers the marker and its chain, A's status and governance, whether X exists
    as a Capability or a `MergedCapability`, the held and current edge sets and the source
    baseline's X, so an equal digest of a state that still plans is the previewed state.

    Args:
        state: Fresh split state from `read_capability_split_state`.
        state_digest: The digest bound into the signed approval.

    Returns:
        True when the guard holds and the split may proceed; False when the state moved or no
        longer plans (the marker is gone, X exists, A is gone).
    """
    try:
        plan = plan_capability_split(state)
    except GraphCleanupValidationError:
        return False
    return plan.state_digest == state_digest


def _split_sources(edge_set: SplitEdgeSet) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The sources that get an edge on X, and those whose edge on A is deleted."""
    return (*edge_set.moves, *edge_set.shared, *edge_set.added), edge_set.moves


def build_capability_split_group(
    plan: CapabilitySplitPlan, *, created_at: str
) -> tuple[Primitive, ...]:
    """The edit: mint X, give it its edges, take the moved ones off A, delete the marker.

    In order: X is upserted as a Capability with the source baseline's properties, its embedding
    and `created_at`; every source of the moves, shared and added sets gets an edge on X; every
    source of the moves set loses its edge on A (by identity); the `ResolvedCapability` marker is
    deleted last, in the same group, so a minted X without the marker removal cannot be logged.
    `GOVERNED_BY` is never named: A keeps its Policy and X starts ungoverned.

    Args:
        plan: The approved split plan.
        created_at: ISO 8601 time recorded on the minted Capability.

    Returns:
        The ordered primitives of the group.
    """
    x_id, a_id = plan.preview.x_id, plan.preview.a_id
    minted = plan.minted
    properties: dict[str, object] = {"name": minted.name, "created_at": created_at}
    if minted.confidence is not None:
        properties["confidence"] = minted.confidence
    if minted.description is not None:
        properties["description"] = minted.description
    x_ref = NodeRef(label="Capability", id=x_id)
    a_ref = NodeRef(label="Capability", id=a_id)
    edges = plan.preview.edges
    by_type = {
        "REQUIRES": edges.requires,
        "COVERS": edges.covers,
        "MITIGATED_BY": edges.mitigated_by,
    }
    primitives: list[Primitive] = [
        UpsertNode(label="Capability", id=x_id, properties=properties, embedding=minted.embedding)
    ]
    for rel_type, source_label in EDGE_CLASSES:
        on_x, _off_a = _split_sources(by_type[rel_type])
        primitives.extend(
            UpsertEdge(
                type=rel_type,
                identity=edge_identity(rel_type, source_id, x_id),
                source=NodeRef(label=source_label, id=source_id),
                target=x_ref,
            )
            for source_id in on_x
        )
    for rel_type, source_label in EDGE_CLASSES:
        _on_x, off_a = _split_sources(by_type[rel_type])
        primitives.extend(
            DeleteEdge(
                type=rel_type,
                identity=edge_identity(rel_type, source_id, a_id),
                source=NodeRef(label=source_label, id=source_id),
                target=a_ref,
            )
            for source_id in off_a
        )
    primitives.append(DeleteNode(label="ResolvedCapability", id=x_id))
    return tuple(primitives)


def write_capability_split(
    graph: GraphHandle,
    baselines: Sequence[tuple[str, GraphHandle]],
    target: GraphWriteTarget,
    *,
    audit_event_id: str,
    x_id: str,
    state_digest: str,
    created_at: str,
) -> None:
    """Split the resolved Capability `x_id` out of its terminal as one gateway group.

    Args:
        graph: Handle on the single-tenant graph, used for the guard read.
        baselines: `(graph name, read handle)` of every `{short}_baseline` graph, read only.
        target: Gateway and graph the group goes to.
        audit_event_id: Id of the `applied` audit row the group is linked to.
        x_id: Id of the `ResolvedCapability` to split.
        state_digest: The digest bound into the signed approval.
        created_at: ISO 8601 time recorded on the minted Capability.

    Side Effects:
        May log an edge-identity stamp group, then submits one group through the gateway.

    Raises:
        GraphCleanupStaleStateError: the guard did not hold (the marker, X, A, its Policy, an edge
            set or the source baseline differs from the previewed state) or the log position
            moved; nothing of the split group was logged.
        GraphCleanupPersistenceError: a graph could not be read or the gateway failed.
    """
    require_caught_up(target)
    expected_position = read_log_position(target)
    state = read_capability_split_state(graph, baselines, x_id)
    if not capability_split_guard_holds(state, state_digest=state_digest):
        raise GraphCleanupStaleStateError(_CAPABILITY_SPLIT_STALE_MESSAGE)
    with refuse_unstorable_values():
        primitives = build_capability_split_group(
            plan_capability_split(state), created_at=created_at
        )
    stamped = ensure_stamped(target, audit_event_id)
    submit_cleanup_group(
        target,
        primitives,
        audit_event_id=audit_event_id,
        stale_message=_CAPABILITY_SPLIT_STALE_MESSAGE,
        preconditions=(ExpectedPosition(position=expected_position + stamped),),
    )
