"""Drop the mutations of a group that would not change the graph (issue #206, AC-BI-004).

The graph state a group names is read once (batched, see `graph_reader`); the group is then
walked over an overlay of that state, so every primitive sees the effect of the ones before it.
A primitive is dropped when applying it would leave the overlay as it is. The same walk decides
the rejections: an edge endpoint or a merge target that exists nowhere is a `MissingTargetError`
(F6), whereas removing from or deleting something absent is a plain no-op.

Values compare by type and value: `1` differs from `1.0`, `True` from `1`, `-0.0` from `0.0`,
and list order matters, so only a mutation that really changes the stored value is dropped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.graph_gateway.errors import MissingTargetError
from ps_service.graph_gateway.graph_reader import (
    EdgeKey,
    NodeKey,
    read_edges,
    read_existing_nodes,
    read_nodes,
)
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    MergeProperty,
    RemoveProperty,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from ps_service.graph_gateway.models import MutationGroup, Primitive
    from ps_service.ingestion.falkordb_client import GraphHandle

_MISSING_TARGET_MESSAGE = "primitive {index} names a node that does not exist"
_EMBEDDING = "embedding"


def select_effective_primitives(
    open_graph: Callable[[], GraphHandle], group: MutationGroup, *, batch_size: int
) -> tuple[Primitive, ...]:
    """Return the primitives of `group` that change the graph, in order.

    Raises:
        MissingTargetError: an upserted edge's endpoint or a merge target exists nowhere.
    """
    graph = open_graph()
    acted_on = set(_acted_on_nodes(group))
    endpoints = set(_edge_endpoints(group)) - acted_on
    nodes = read_nodes(graph, acted_on, batch_size)
    nodes.update(
        {key: {} for key in read_existing_nodes(graph, endpoints, batch_size)}
    )  # an endpoint nothing else in the group touches: its existence is all that is used
    overlay = _Overlay(nodes, read_edges(graph, _named_edges(group), batch_size))
    return tuple(
        primitive
        for index, primitive in enumerate(group.primitives)
        if overlay.apply(index, primitive)
    )


def _edge_key(edge: UpsertEdge | DeleteEdge) -> EdgeKey:
    return (
        edge.type,
        edge.source.label,
        edge.source.id,
        edge.target.label,
        edge.target.id,
        edge.identity,
    )


def _acted_on_nodes(group: MutationGroup) -> Iterator[NodeKey]:
    """Yield every node a node primitive of `group` acts on: their state decides the no-ops."""
    for primitive in group.primitives:
        if not isinstance(primitive, UpsertEdge | DeleteEdge):
            yield (primitive.label, primitive.id)


def _edge_endpoints(group: MutationGroup) -> Iterator[NodeKey]:
    """Yield both endpoints of every edge primitive of `group`: only their existence matters."""
    for primitive in group.primitives:
        if isinstance(primitive, UpsertEdge | DeleteEdge):
            yield (primitive.source.label, primitive.source.id)
            yield (primitive.target.label, primitive.target.id)


def _named_edges(group: MutationGroup) -> Iterator[EdgeKey]:
    """Yield the key of every edge a primitive of `group` upserts or deletes."""
    for primitive in group.primitives:
        if isinstance(primitive, UpsertEdge | DeleteEdge):
            yield _edge_key(primitive)


def _same_value(left: object, right: object) -> bool:
    """Return whether two property values are the same stored value (type, value, list order)."""
    left_items, right_items = _as_sequence(left), _as_sequence(right)
    if left_items is not None and right_items is not None:
        return len(left_items) == len(right_items) and all(
            _same_value(a, b) for a, b in zip(left_items, right_items, strict=True)
        )
    if isinstance(left, float) and isinstance(right, float):
        return left.hex() == right.hex()
    return type(left) is type(right) and left == right


def _as_sequence(value: object) -> Sequence[object] | None:
    """Return `value` as a sequence if it is a stored list (a list or tuple), else None."""
    if isinstance(value, list | tuple):
        return cast("Sequence[object]", value)  # a list of property scalars
    return None


def _already_holds(current: Mapping[str, object], wanted: Mapping[str, object]) -> bool:
    """Return whether every wanted property is already stored with the same value."""
    return all(key in current and _same_value(current[key], value) for key, value in wanted.items())


class _Overlay:
    """The graph state a group sees: what was read, changed by the primitives walked so far."""

    def __init__(
        self,
        nodes: Mapping[NodeKey, dict[str, object]],
        edges: Mapping[EdgeKey, dict[str, object]],
    ) -> None:
        self._nodes = dict(nodes)
        self._edges = dict(edges)

    def apply(self, index: int, primitive: Primitive) -> bool:
        """Apply `primitive` to the overlay and return whether it changed anything."""
        match primitive:
            case UpsertNode():
                return self._upsert_node(primitive)
            case MergeProperty():
                return self._merge_property(index, primitive)
            case RemoveProperty():
                return self._remove_property(primitive)
            case DeleteNode():
                return self._delete_node(primitive)
            case UpsertEdge():
                return self._upsert_edge(index, primitive)
            case DeleteEdge():
                return self._edges.pop(_edge_key(primitive), None) is not None

    def _upsert_node(self, node: UpsertNode) -> bool:
        wanted = dict(node.properties)
        if node.embedding is not None:
            wanted[_EMBEDDING] = list(node.embedding)
        key = (node.label, node.id)
        current = self._nodes.get(key)
        if current is not None and _already_holds(current, wanted):
            return False
        self._nodes[key] = {**(current or {}), **wanted}
        return True

    def _merge_property(self, index: int, merge: MergeProperty) -> bool:
        key = (merge.label, merge.id)
        current = self._require_node(index, key)
        if _already_holds(current, merge.properties):
            return False
        current.update(merge.properties)
        return True

    def _remove_property(self, removal: RemoveProperty) -> bool:
        current = self._nodes.get((removal.label, removal.id))
        if current is None:
            return False
        present = [key for key in removal.keys if key in current]
        for key in present:
            del current[key]
        return bool(present)

    def _delete_node(self, deletion: DeleteNode) -> bool:
        key = (deletion.label, deletion.id)
        if self._nodes.pop(key, None) is None:
            return False
        self._edges = {
            edge: properties
            for edge, properties in self._edges.items()
            if key not in {(edge[1], edge[2]), (edge[3], edge[4])}
        }
        return True

    def _upsert_edge(self, index: int, edge: UpsertEdge) -> bool:
        self._require_node(index, (edge.source.label, edge.source.id))
        self._require_node(index, (edge.target.label, edge.target.id))
        key = _edge_key(edge)
        current = self._edges.get(key)
        if current is not None and _already_holds(current, edge.properties):
            return False
        self._edges[key] = {**(current or {}), **edge.properties}
        return True

    def _require_node(self, index: int, key: NodeKey) -> dict[str, object]:
        current = self._nodes.get(key)
        if current is None:
            raise MissingTargetError(_MISSING_TARGET_MESSAGE.format(index=index))
        return current
