"""An artifact's native or baseline leg as gateway primitives.

Restore hands the gateway a leg's content as `UpsertNode` and `UpsertEdge` primitives; the
gateway's replace semantics (a replacing `GraphLeg`) make the graph hold exactly that content.
Nothing here reads or writes a graph.

The leg is folded first so the gateway's `MissingTargetError` never fires for it: an edge whose
endpoint is absent from the artifact is dropped, a repeated edge folds into one (the last
occurrence's properties win) and a repeated node id keeps its last occurrence. An edge's
`identity` is recomputed as `TYPE:source->target`; an artifact that carries a different one, or
any value the gateway cannot store, is an `ArtifactContentRejectedError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from ps_service.export.models import SerializedGraph
from ps_service.graph_gateway.models import NodeRef, UpsertEdge, UpsertNode
from ps_service.restore.errors import ArtifactContentRejectedError

if TYPE_CHECKING:
    from ps_service.export.models import SerializedEdge, SerializedNode
    from ps_service.graph_gateway.models import Primitive

__all__ = ["embeddings_by_label", "fold_serialized_graph", "leg_primitives"]

_EMBEDDING = "embedding"
_IDENTITY = "identity"


def _node_id(node: SerializedNode) -> str:
    """The node's string `id` property; a missing or non-string one rejects the artifact."""
    node_id = node.properties.get("id")
    if not isinstance(node_id, str):
        raise ArtifactContentRejectedError(
            f"node with label {node.label!r} has a missing or non-string 'id' property: {node_id!r}"
        )
    return node_id


def edge_identity(edge: SerializedEdge) -> str:
    """The gateway identity of `edge`: `TYPE:source->target`, one per (type, endpoints).

    Args:
        edge: A serialized edge with its relationship type and endpoint ids.

    Returns:
        The identity string the gateway keys the edge by.
    """
    return f"{edge.relationship_type}:{edge.source_id}->{edge.target_id}"


def fold_serialized_graph(graph: SerializedGraph) -> SerializedGraph:
    """`graph` reduced to the content a written graph would hold.

    Repeated node ids keep their last occurrence (at the first occurrence's place), an edge with
    an endpoint the artifact does not hold is dropped, and repeated edges fold into one that keeps
    the last occurrence's properties.

    Args:
        graph: A parsed artifact leg.

    Returns:
        A new `SerializedGraph` with unique nodes (by label and id) and unique, fully anchored
        edges; empty when `graph` is empty.

    Raises:
        ArtifactContentRejectedError: a node has a missing or non-string `id` property.
    """
    nodes: dict[tuple[str, str], SerializedNode] = {}
    for node in graph.nodes:
        nodes[(node.label, _node_id(node))] = node
    edges: dict[tuple[str, str, str, str, str], SerializedEdge] = {}
    for edge in graph.edges:
        if (edge.source_label, edge.source_id) not in nodes:
            continue
        if (edge.target_label, edge.target_id) not in nodes:
            continue
        key = (
            edge.relationship_type,
            edge.source_label,
            edge.source_id,
            edge.target_label,
            edge.target_id,
        )
        edges[key] = edge
    return SerializedGraph(nodes=tuple(nodes.values()), edges=tuple(edges.values()))


def embeddings_by_label(graph: SerializedGraph, label: str) -> dict[str, tuple[float, ...]]:
    """Every `label` node's artifact-supplied embedding, by id (a node without one is absent).

    `graph_reader.read_baseline_graph` reads no Policy embedding and a live baseline carries
    none, so the artifact's parsed blob is the only place a restore recovers these vectors from.

    Args:
        graph: A parsed artifact leg.
        label: Node label to collect embeddings for, for example `"Policy"`.

    Returns:
        Map of node id to embedding vector; empty when no node of `label` carries one.

    Raises:
        ArtifactContentRejectedError: a node of `label` with an embedding has no string `id`.
    """
    return {
        _node_id(node): tuple(cast("list[float]", node.properties[_EMBEDDING]))
        for node in graph.nodes
        if node.label == label and node.properties.get(_EMBEDDING) is not None
    }


def _upsert_node(node: SerializedNode) -> UpsertNode:
    """The node as an `UpsertNode`; a property the gateway cannot store rejects the artifact."""
    node_id = _node_id(node)
    properties: dict[str, object] = {
        key: value for key, value in node.properties.items() if key not in {"id", _EMBEDDING}
    }
    raw_embedding = node.properties.get(_EMBEDDING)
    try:
        return UpsertNode(
            label=node.label,
            id=node_id,
            properties=properties,
            embedding=None if raw_embedding is None else tuple(cast("list[float]", raw_embedding)),
        )
    except (ValidationError, TypeError) as exc:
        raise ArtifactContentRejectedError(
            f"{node.label} {node_id!r} has a property the graph cannot store "
            "(a reserved key, an empty list, or a value that is not finite or is out of range)"
        ) from exc


def _upsert_edge(edge: SerializedEdge) -> UpsertEdge:
    """The edge as an `UpsertEdge` keyed by its recomputed identity; a foreign identity rejects."""
    identity = edge_identity(edge)
    properties: dict[str, object] = dict(edge.properties)
    authored = properties.pop(_IDENTITY, identity)
    if authored != identity:
        raise ArtifactContentRejectedError(
            f"{edge.relationship_type} edge {edge.source_id!r} -> {edge.target_id!r} carries "
            f"the identity {authored!r}, which is not {identity!r}"
        )
    try:
        return UpsertEdge(
            type=edge.relationship_type,
            identity=identity,
            source=NodeRef(label=edge.source_label, id=edge.source_id),
            target=NodeRef(label=edge.target_label, id=edge.target_id),
            properties=properties,
        )
    except ValidationError as exc:
        raise ArtifactContentRejectedError(
            f"{edge.relationship_type} edge {edge.source_id!r} -> {edge.target_id!r} has a "
            "property the graph cannot store (a reserved key, an empty list, or a value that is "
            "not finite or is out of range)"
        ) from exc


def leg_primitives(graph: SerializedGraph) -> tuple[Primitive, ...]:
    """Every node, then every edge, of the folded `graph` as gateway primitives.

    Args:
        graph: A parsed artifact leg (native or baseline).

    Returns:
        Upsert primitives, nodes first and then edges; empty when the folded leg has no content.

    Raises:
        ArtifactContentRejectedError: a node has no string id, an edge carries an identity other
            than the formula, or a node or edge holds a value the gateway cannot store.
    """
    folded = fold_serialized_graph(graph)
    return (
        *(_upsert_node(node) for node in folded.nodes),
        *(_upsert_edge(edge) for edge in folded.edges),
    )
