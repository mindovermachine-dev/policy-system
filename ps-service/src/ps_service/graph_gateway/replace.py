"""Expand a leg of a multi-graph group into the logged primitives that make it exact.

A leg that merges into its graph needs only one thing ahead of its own primitives: a graph written
outside the gateway may hold edges with no `identity`, and the gateway merges edges on it, so a
write onto such a graph would add parallel edges. Those edges are stamped inside the same set
(never in a group of their own: a rejected set must leave no entry behind).

A leg that REPLACES its graph also has to remove whatever the leg does not contain, and an upsert
can only add or overwrite. The replacement is therefore expressed with the primitives that already
exist, so the log, the applier, the digest and replay stay as they are:

1. `StampEdgeIdentity` for every identity-less edge that survives (the identity is what
   `DeleteEdge` and a later upsert address it by);
2. `DeleteEdge` for every surviving edge the leg does not upsert, and for one that holds a property
   name the leg's edge lacks (an edge has no property-removal primitive: it is recreated);
3. `DeleteNode` for every live node the leg does not upsert, and for one that holds a property
   name the leg's node lacks (`RemoveProperty` cannot remove `id` or `embedding`: it is
   recreated). The delete takes the node's edges with it, so an edge at a deleted node needs
   neither a stamp nor a delete, and the leg's own `UpsertEdge` brings back the ones it keeps.

Everything else is left to the caller's upserts, which the no-op filter then drops where the graph
already matches: replacing a graph by its own content logs nothing. Values are never compared
here, only property names, so a changed value is simply overwritten by its upsert.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_gateway.edge_identity_backfill import (
    legacy_edge_stamp,
    scan_unidentified_edges,
)
from ps_service.graph_gateway.graph_reader import UNIDENTIFIED, read_graph_shape
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    MutationGroup,
    NodeRef,
    StampEdgeIdentity,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from ps_service.graph_gateway.connection import WritableGraph
    from ps_service.graph_gateway.graph_reader import EdgeKey, GraphShape, NodeKey
    from ps_service.graph_gateway.models import Primitive

_EMBEDDING = "embedding"


def expand_leg(graph: WritableGraph, group: MutationGroup, *, replace: bool) -> MutationGroup:
    """Return `group` with the stamps (and, when replacing, the deletes) placed before its own.

    Reads `graph` (read-only); the caller holds the graph's lock, so what is read is what the
    group is logged against. The group is returned as it is when nothing is to be added.

    Args:
        graph: The leg's live graph; it is only read.
        group: The leg's own primitives.
        replace: True if the leg replaces the whole graph, so whatever the leg does not contain
            is deleted; False if it merges, so only identity-less edges are stamped.

    Returns:
        `group` itself when nothing is added; otherwise a group with the added primitives ahead
        of the leg's own and every other field unchanged.

    Raises:
        UnexpectedGraphReplyError: A read of `graph` answered an unexpected shape.

    Side Effects:
        Runs read queries on `graph`; it writes nothing.
    """
    added = (
        _replacement(read_graph_shape(graph), group)
        if replace
        else tuple(legacy_edge_stamp(*edge) for edge in scan_unidentified_edges(graph)[0])
    )
    if not added:
        return group
    return MutationGroup(
        graph=group.graph,
        audit_event_id=group.audit_event_id,
        primitives=(*added, *group.primitives),
        preconditions=group.preconditions,
        checkpoint_requested=group.checkpoint_requested,
    )


def added_count(original: MutationGroup, expanded: MutationGroup) -> int:
    """Return how many primitives the expansion put ahead of the caller's.

    Args:
        original: The group as the caller built it.
        expanded: The group returned by `expand_leg` for `original`.

    Returns:
        The number of added primitives; 0 when the group was returned as it was.
    """
    return len(expanded.primitives) - len(original.primitives)


def _replacement(live: GraphShape, group: MutationGroup) -> tuple[Primitive, ...]:
    """Return the primitives that bring `live` to the point where only `group`'s upserts remain."""
    wanted_nodes = _wanted_node_names(group)
    wanted_edges = _wanted_edge_names(group)
    dropped = {
        key
        for key, names in live.nodes.items()
        if key not in wanted_nodes or not names <= wanted_nodes[key]
    }
    stamps: list[StampEdgeIdentity] = []
    edge_deletes: list[DeleteEdge] = []
    for key, names in sorted(live.edges.items()):
        kind, source_label, source_id, target_label, target_id, identity = key
        if (source_label, source_id) in dropped or (target_label, target_id) in dropped:
            continue  # goes with its node
        source, target = (
            NodeRef(label=source_label, id=source_id),
            NodeRef(label=target_label, id=target_id),
        )
        if identity == UNIDENTIFIED:
            stamp = legacy_edge_stamp(kind, source, target)
            stamps.append(stamp)
            identity = stamp.identity
        keeps = wanted_edges.get((kind, source_label, source_id, target_label, target_id, identity))
        if keeps is None or not names <= keeps:
            edge_deletes.append(
                DeleteEdge(type=kind, identity=identity, source=source, target=target)
            )
    node_deletes = [DeleteNode(label=label, id=node_id) for label, node_id in sorted(dropped)]
    return (*stamps, *edge_deletes, *node_deletes)


def _wanted_node_names(group: MutationGroup) -> dict[NodeKey, frozenset[str]]:
    """Property names the leg gives each upserted node (`embedding` counts if the node has one)."""
    wanted: dict[NodeKey, set[str]] = {}
    for primitive in group.primitives:
        if isinstance(primitive, UpsertNode):
            names = wanted.setdefault((primitive.label, primitive.id), set())
            names.update(primitive.properties)
            if primitive.embedding is not None:
                names.add(_EMBEDDING)
    return {key: frozenset(names) for key, names in wanted.items()}


def _wanted_edge_names(group: MutationGroup) -> dict[EdgeKey, frozenset[str]]:
    """Property names the leg gives each edge it upserts."""
    wanted: dict[EdgeKey, set[str]] = {}
    for primitive in group.primitives:
        if isinstance(primitive, UpsertEdge):
            key = (
                primitive.type,
                primitive.source.label,
                primitive.source.id,
                primitive.target.label,
                primitive.target.id,
                primitive.identity,
            )
            wanted.setdefault(key, set()).update(primitive.properties)
    return {key: frozenset(names) for key, names in wanted.items()}
