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
    node_state_query,
    node_state_rows,
)
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from ps_service.ingestion.falkordb_client import GraphHandle

type NodeKey = tuple[str, str]
"""`(label, id)` of a node."""

type EdgeKey = tuple[str, str, str, str, str, str]
"""`(type, source label, source id, target label, target id, identity)` of an edge."""

_REPLY_MESSAGE = "the graph answered a state read with an unexpected shape"

_ID = "id"
_IDENTITY = "identity"


def read_nodes(
    graph: GraphHandle, nodes: Iterable[NodeKey], batch_size: int
) -> dict[NodeKey, dict[str, object]]:
    """Return the properties (minus `id`) of each of `nodes` that the graph holds."""
    ids_by_label: dict[str, list[str]] = {}
    for label, node_id in sorted(set(nodes)):
        ids_by_label.setdefault(label, []).append(node_id)
    found: dict[NodeKey, dict[str, object]] = {}
    for label, ids in ids_by_label.items():
        query = node_state_query(label)
        for chunk in _chunks(ids, batch_size):
            result = graph.query(query, {"rows": node_state_rows(chunk)})
            for row in (_row(item, 2) for item in result.result_set):
                found[(label, _text(row[0]))] = _properties(row[1], drop=_ID)
    return found


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
            for row in (_row(item, 4) for item in result.result_set):
                key = (
                    relationship_type,
                    source_label,
                    _text(row[0]),
                    target_label,
                    _text(row[1]),
                    _text(row[2]),
                )
                found[key] = _properties(row[3], drop=_IDENTITY)
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


def _properties(value: object, *, drop: str) -> dict[str, object]:
    """Return a property map without the key the gateway owns (`id` or `identity`)."""
    if not isinstance(value, dict):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    properties = cast("dict[str, object]", value)  # a property map; keys are strings
    return {key: item for key, item in properties.items() if key != drop}
