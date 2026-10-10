"""Batched reads of the current state of the nodes and edges a group names (issue #206).

Reads are read-only, parameterized and chunked at the batch size, one query per label (nodes) or
per relationship type and endpoint labels (edges). The reply shape is checked: anything else is
an `UnexpectedGraphReplyError` that names nothing the graph returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.graph_gateway.cypher import (
    edge_state_query,
    edge_state_rows,
    node_exists_query,
    node_state_query,
    node_state_rows,
)
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError
from ps_service.graph_gateway.exact_floats import PROPERTY_COLUMN_COUNT, restore_properties

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from ps_service.ingestion.falkordb_client import GraphHandle

type NodeKey = tuple[str, str]
"""`(label, id)` of a node."""

type EdgeKey = tuple[str, str, str, str, str, str]
"""`(type, source label, source id, target label, target id, identity)` of an edge."""

STATE_READ_CHUNK_ROWS = 100
"""Most rows one node state read asks for: a state row can carry a 3,072-double embedding."""

_REPLY_MESSAGE = "the graph answered a state read with an unexpected shape"

_NODE_WIDTH = 5
"""A node state row: id and the four property columns."""
_EDGE_WIDTH = 7
"""An edge state row: source id, target id, identity and the four property columns."""

_ID = "id"
_IDENTITY = "identity"


def read_nodes(
    graph: GraphHandle, nodes: Iterable[NodeKey], batch_size: int
) -> dict[NodeKey, dict[str, object]]:
    """Return the properties (minus `id`) of each of `nodes` that the graph holds.

    A statement asks for at most `STATE_READ_CHUNK_ROWS` rows (and `batch_size`), because the
    reply carries every property, embeddings included.
    """
    found: dict[NodeKey, dict[str, object]] = {}
    for label, ids in _ids_by_label(nodes).items():
        query = node_state_query(label)
        for chunk in _chunks(ids, min(batch_size, STATE_READ_CHUNK_ROWS)):
            result = graph.query(query, {"rows": node_state_rows(chunk)})
            for row in (_row(item, _NODE_WIDTH) for item in result.result_set):
                found[(label, _text(row[0]))] = _properties(row[1:], drop=_ID)
    return found


def read_existing_nodes(
    graph: GraphHandle, nodes: Iterable[NodeKey], batch_size: int
) -> set[NodeKey]:
    """Return which of `nodes` the graph holds, without reading any of their properties."""
    found: set[NodeKey] = set()
    for label, ids in _ids_by_label(nodes).items():
        query = node_exists_query(label)
        for chunk in _chunks(ids, batch_size):
            result = graph.query(query, {"rows": node_state_rows(chunk)})
            for row in result.result_set:
                found.add((label, _text(_row(row, 1)[0])))
    return found


def _ids_by_label(nodes: Iterable[NodeKey]) -> dict[str, list[str]]:
    """Group the distinct node keys by label, ids sorted (a stable query order)."""
    ids_by_label: dict[str, list[str]] = {}
    for label, node_id in sorted(set(nodes)):
        ids_by_label.setdefault(label, []).append(node_id)
    return ids_by_label


def read_edges(
    graph: GraphHandle, edges: Iterable[EdgeKey], batch_size: int
) -> dict[EdgeKey, dict[str, object]]:
    """Return the properties (minus `identity`) of each of `edges` that the graph holds."""
    by_shape: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    for relationship_type, source_label, source_id, target_label, target_id, identity in sorted(
        set(edges)
    ):
        shape = (relationship_type, source_label, target_label)
        by_shape.setdefault(shape, []).append((source_id, target_id, identity))
    found: dict[EdgeKey, dict[str, object]] = {}
    for (relationship_type, source_label, target_label), members in by_shape.items():
        query = edge_state_query(relationship_type, source_label, target_label)
        for chunk in _chunks(members, batch_size):
            result = graph.query(query, {"rows": edge_state_rows(chunk)})
            for row in (_row(item, _EDGE_WIDTH) for item in result.result_set):
                key = (
                    relationship_type,
                    source_label,
                    _text(row[0]),
                    target_label,
                    _text(row[1]),
                    _text(row[2]),
                )
                found[key] = _properties(row[3:], drop=_IDENTITY)
    return found


def _chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Yield consecutive slices of `items` of at most `size`."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _row(row: object, width: int) -> list[object]:
    """Return a result row as a list of `width` items, or raise if the driver gave anything else."""
    if not isinstance(row, list) or len(cast("list[object]", row)) != width:
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return cast("list[object]", row)  # narrowed to a list; its items are checked on use


def _text(value: object) -> str:
    """Return `value` as text, or raise if the graph answered a non-string id."""
    if not isinstance(value, str):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return value


def _properties(columns: list[object], *, drop: str) -> dict[str, object]:
    """Return the exact property map (minus `id` or `identity`) from the four property columns."""
    if len(columns) != PROPERTY_COLUMN_COUNT:
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    exact = restore_properties(*columns)
    return {key: item for key, item in exact.items() if key != drop}
