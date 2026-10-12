"""Batched reads of the current state of the nodes and edges a group names (issue #206).

Reads are read-only, parameterized and chunked at the batch size, one query per label (nodes) or
per relationship type and endpoint labels (edges). The reply shape is checked: anything else is
an `UnexpectedGraphReplyError` that names nothing the graph returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ps_service.graph_gateway.cypher import (
    DIGEST_EDGE_SCAN,
    DIGEST_NODE_SCAN,
    edge_state_query,
    edge_state_rows,
    node_exists_query,
    node_state_query,
    node_state_rows,
    unidentified_edge_state_query,
    unidentified_edge_state_rows,
)
from ps_service.graph_gateway.digest import DigestSettings
from ps_service.graph_gateway.errors import UnexpectedGraphReplyError
from ps_service.graph_gateway.exact_floats import PROPERTY_COLUMN_COUNT, restore_properties

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from ps_service.ingestion.falkordb_client import GraphHandle

type NodeKey = tuple[str, str]
"""`(label, id)` of a node."""

type EdgeKey = tuple[str, str, str, str, str, str]
"""`(type, source label, source id, target label, target id, identity)` of an edge."""

UNIDENTIFIED = ""
"""The identity slot of an edge key for an edge that has none (a real identity is never blank)."""

STATE_READ_CHUNK_ROWS = 100
"""Most rows one node state read asks for: a state row can carry a 3,072-double embedding."""

_REPLY_MESSAGE = "the graph answered a state read with an unexpected shape"

_NODE_WIDTH = 5
"""A node state row: id and the four property columns."""
_UNIDENTIFIED_EDGE_WIDTH = 6
"""An identity-less edge state row: source id, target id and the four property columns."""
_EDGE_WIDTH = 7
"""An edge state row: source id, target id, identity and the four property columns."""

_ID = "id"
_IDENTITY = "identity"

_SCAN_NODE_WIDTH = 6
"""A node scan row: internal id, label list and the four property columns."""
_SCAN_EDGE_WIDTH = 10
"""An edge scan row: internal id, type, source labels and id, target labels and id, properties."""

type LegacyEdgeKey = tuple[str, str, str, str, str]
"""`(type, source label, source id, target label, target id)` of an edge that has no identity."""


@dataclass(frozen=True, slots=True)
class PersistedElements:
    """What one graph holds, by key only: no property is kept (issue #99).

    The replay sentinel is not an element. An edge is in `edges` when it carries an identity and
    in `legacy_edges` when a pre-gateway writer left it without one.
    """

    nodes: frozenset[NodeKey]
    edges: frozenset[EdgeKey]
    legacy_edges: frozenset[LegacyEdgeKey]
    log_position: int
    """The graph log's last position when the read started (0: nothing logged)."""


def read_persisted_elements(
    graph: GraphHandle, *, log_position: int, settings: DigestSettings | None = None
) -> PersistedElements:
    """Enumerate the node keys and edge keys of `graph` with two chunked read-only scans.

    Raises:
        UnexpectedGraphReplyError: a row has an unexpected shape, or names a node that carries
            no string `id` or more or fewer than one label (it cannot be classified).
    """
    chosen = settings if settings is not None else DigestSettings()
    nodes = frozenset(_scan(graph, DIGEST_NODE_SCAN, chosen.node_chunk_rows, _node_key))
    keys = list(_scan(graph, DIGEST_EDGE_SCAN, chosen.edge_chunk_rows, _edge_key))
    return PersistedElements(
        nodes=nodes,
        edges=frozenset(key for key in keys if key[5] != UNIDENTIFIED),
        legacy_edges=frozenset(_legacy(key) for key in keys if key[5] == UNIDENTIFIED),
        log_position=log_position,
    )


@dataclass(frozen=True, slots=True)
class GraphShape:
    """What one graph holds, by key and by property NAMES only: no property value is kept.

    What a replacing write needs to tell an element that already matches from one that holds
    something the new content lacks, without keeping a 3,072-double embedding per node in memory.
    A node's names exclude `id`; an edge's exclude `identity`, and an edge with no identity sits
    under the `UNIDENTIFIED` slot of its key.
    """

    nodes: dict[NodeKey, frozenset[str]]
    edges: dict[EdgeKey, frozenset[str]]


def read_graph_shape(graph: GraphHandle, *, settings: DigestSettings | None = None) -> GraphShape:
    """Read the key and property names of every node and edge of `graph` (read-only, chunked).

    Raises:
        UnexpectedGraphReplyError: a row has an unexpected shape, or names a node that carries
            no string `id` or more or fewer than one label.
    """
    chosen = settings if settings is not None else DigestSettings()
    return GraphShape(
        nodes=dict(_scan(graph, DIGEST_NODE_SCAN, chosen.node_chunk_rows, _node_shape)),
        edges=dict(_scan(graph, DIGEST_EDGE_SCAN, chosen.edge_chunk_rows, _edge_shape)),
    )


def _node_shape(row: object) -> tuple[int, tuple[NodeKey, frozenset[str]]]:
    cursor, labels, *columns = _row(row, _SCAN_NODE_WIDTH)
    properties = _properties(columns, drop="")
    names = frozenset(properties) - {_ID}
    return _cursor(cursor), ((_single_label(labels), _text(properties.get(_ID))), names)


def _edge_shape(row: object) -> tuple[int, tuple[EdgeKey, frozenset[str]]]:
    cursor, key = _edge_key(row)
    columns = _row(row, _SCAN_EDGE_WIDTH)[-PROPERTY_COLUMN_COUNT:]
    return cursor, (key, frozenset(_properties(columns, drop="")) - {_IDENTITY})


def _legacy(key: EdgeKey) -> LegacyEdgeKey:
    return (key[0], key[1], key[2], key[3], key[4])


def _scan[T](
    graph: GraphHandle, query: str, chunk_rows: int, decode: Callable[[object], tuple[int, T]]
) -> Iterator[T]:
    """Yield what `decode` makes of each scanned row, chunk by chunk along the id cursor."""
    after = -1
    while True:
        rows = graph.query(query, {"after": after, "limit": chunk_rows}).result_set
        for row in rows:
            cursor, element = decode(row)
            after = max(after, cursor)
            yield element
        if len(rows) < chunk_rows:
            return


def _node_key(row: object) -> tuple[int, NodeKey]:
    cursor, labels, *columns = _row(row, _SCAN_NODE_WIDTH)
    properties = _properties(columns, drop="")
    return _cursor(cursor), (_single_label(labels), _text(properties.get(_ID)))


def _edge_key(row: object) -> tuple[int, EdgeKey]:
    cursor, kind, source_labels, source_id, target_labels, target_id, *columns = _row(
        row, _SCAN_EDGE_WIDTH
    )
    identity = _properties(columns, drop="").get(_IDENTITY, UNIDENTIFIED)
    return _cursor(cursor), (
        _text(kind),
        _single_label(source_labels),
        _text(source_id),
        _single_label(target_labels),
        _text(target_id),
        _text(identity),
    )


def _cursor(value: object) -> int:
    if not isinstance(value, int):
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return value


def _single_label(labels: object) -> str:
    """Return the one label of a node, or raise: a node with none or several is unclassifiable."""
    if not isinstance(labels, list) or len(cast("list[object]", labels)) != 1:
        raise UnexpectedGraphReplyError(_REPLY_MESSAGE)
    return _text(cast("list[object]", labels)[0])


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


def read_unidentified_edges(
    graph: GraphHandle, edges: Iterable[EdgeKey], batch_size: int
) -> dict[EdgeKey, dict[str, object]]:
    """Return the properties of each of `edges` (identity ignored) that the graph holds without one.

    Keys come back with an empty identity. Several identity-less edges between the same two nodes
    read as one (the last), which is all a stamp needs: it gives every one of them the identity.
    """
    by_shape: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for relationship_type, source_label, source_id, target_label, target_id, _ in sorted(
        set(edges)
    ):
        shape = (relationship_type, source_label, target_label)
        by_shape.setdefault(shape, []).append((source_id, target_id))
    found: dict[EdgeKey, dict[str, object]] = {}
    for (relationship_type, source_label, target_label), members in by_shape.items():
        query = unidentified_edge_state_query(relationship_type, source_label, target_label)
        for chunk in _chunks(members, batch_size):
            result = graph.query(query, {"rows": unidentified_edge_state_rows(chunk)})
            for row in (_row(item, _UNIDENTIFIED_EDGE_WIDTH) for item in result.result_set):
                key = (
                    relationship_type,
                    source_label,
                    _text(row[0]),
                    target_label,
                    _text(row[1]),
                    UNIDENTIFIED,
                )
                found[key] = _properties(row[2:], drop=_IDENTITY)
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
