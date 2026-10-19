"""Serialize a graph to canonical JSON, checksum it, and parse it back.

`serialize_graph` reads a whole FalkorDB graph into a `SerializedGraph` generically: it enumerates
the graph's own labels and relationship types and runs one `MATCH` per label or type, instead of
hardcoding a node and edge catalog, so the same function serves the baseline graph and the native
structural graph. `to_json_bytes` and `parse_serialized_graph_json` are the deterministic
JSON <-> `SerializedGraph` codec, and `checksum_bytes` is the SHA-256 hex digest recorded in a
manifest.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol, cast

from ps_service.domain_schema.vocabulary_exceptions import MERGE_MARKER_LABELS
from ps_service.export.errors import ExportSourceGraphError
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.graph_gateway import REPLAY_STATE_LABEL

if TYPE_CHECKING:
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.export.models import SerializedPropertyValue

_NODE_ROWS_QUERY = "MATCH (n:{label}) RETURN labels(n), properties(n)"
_TOTAL_NODE_COUNT_QUERY = "MATCH (n) RETURN count(n)"
_EDGE_ROWS_QUERY = (
    "MATCH (s)-[r:{relationship_type}]->(t) RETURN labels(s), s.id, labels(t), t.id, properties(r)"
)


def checksum_bytes(blob: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of `blob`.

    This is the value a manifest records as `baseline_sha256` or `native_sha256`.

    Args:
        blob: The exact bytes to hash; the empty bytestring is allowed.

    Returns:
        A 64-character hex string.
    """
    return hashlib.sha256(blob).hexdigest()


class _QueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`, limited to the field this module reads."""

    @property
    def result_set(self) -> list[object]: ...


def _rows(
    graph: _GraphQueryHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    """Run a read query on `graph` and return its result rows (single call site for reads)."""
    result = graph.query(query, params)
    return cast("list[list[object]]", cast("_QueryResult", result).result_set)


def _enumerate_labels(graph: _GraphQueryHandle) -> list[str]:
    """Return every node label defined in the graph's schema."""
    return [cast("str", row[0]) for row in _rows(graph, "CALL db.labels()")]


def _enumerate_relationship_types(graph: _GraphQueryHandle) -> list[str]:
    """Return every relationship type defined in the graph's schema."""
    return [cast("str", row[0]) for row in _rows(graph, "CALL db.relationshipTypes()")]


def _properties(raw: object) -> dict[str, SerializedPropertyValue]:
    """Copy a driver property mapping into a plain dict."""
    return cast("dict[str, SerializedPropertyValue]", dict(cast("dict[str, object]", raw)))


def _read_nodes_for_label(graph: _GraphQueryHandle, label: str) -> list[SerializedNode]:
    """Read every node carrying `label`; raise `ExportSourceGraphError` if one has != 1 label."""
    query = _NODE_ROWS_QUERY.format(label=label)
    nodes: list[SerializedNode] = []
    for node_labels_raw, properties_raw in _rows(graph, query):
        node_labels = cast("list[str]", node_labels_raw)
        if len(node_labels) != 1:
            raise ExportSourceGraphError(
                f"node matched via label {label!r} carries {len(node_labels)} label(s) "
                f"{node_labels!r} -- every node must carry exactly one label"
            )
        nodes.append(SerializedNode(label=node_labels[0], properties=_properties(properties_raw)))
    return nodes


def _read_edges_for_relationship_type(
    graph: _GraphQueryHandle, relationship_type: str
) -> list[SerializedEdge]:
    """Read every edge of `relationship_type`; raise `ExportSourceGraphError` on a bad endpoint."""
    query = _EDGE_ROWS_QUERY.format(relationship_type=relationship_type)
    edges: list[SerializedEdge] = []
    for row in _rows(graph, query):
        source_labels = cast("list[str]", row[0])
        target_labels = cast("list[str]", row[2])
        if len(source_labels) != 1 or len(target_labels) != 1:
            raise ExportSourceGraphError(
                f"{relationship_type!r} edge endpoint carries != 1 label "
                f"(source={source_labels!r}, target={target_labels!r})"
            )
        edges.append(
            SerializedEdge(
                relationship_type=relationship_type,
                source_label=source_labels[0],
                source_id=cast("str", row[1]),
                target_label=target_labels[0],
                target_id=cast("str", row[3]),
                properties=_properties(row[4]),
            )
        )
    return edges


def serialize_graph(graph: _GraphQueryHandle) -> SerializedGraph:
    """Read every node and edge out of an already-selected graph.

    The graph's own labels and relationship types are enumerated (`CALL db.labels()` and
    `CALL db.relationshipTypes()`) rather than hardcoded, so one function is correct for both
    the baseline graph and the native structural graph. The cost is one query per label for
    nodes and one per relationship type for edges, not one per node or edge.

    Business Rules:
        Every node and every edge endpoint carries exactly one label. A graph that still holds
        a replay progress record is not exported, because its replay from the mutation log is
        unfinished or failed and nobody has verified its contents. A merge marker
        (`MergedCapability`, `MergedObligation`, `MergedRole`) records a merge made in this
        deployment's own graph and is left out of the artifact.

    Args:
        graph: Read handle on the graph to serialize; the caller has already selected it.

    Returns:
        A `SerializedGraph` with every node and edge, in query order (not sorted). An empty
        graph yields empty tuples.

    Raises:
        ExportSourceGraphError: A node or edge endpoint carries zero or more than one label, or
            the graph holds a replay progress record.
    """
    nodes: list[SerializedNode] = []
    for label in _enumerate_labels(graph):
        nodes.extend(_read_nodes_for_label(graph, label))
    if any(node.label == REPLAY_STATE_LABEL for node in nodes):
        # The label stays in the schema after a replay ends; only a node of it means a replay
        # that is unfinished or failed, whose graph nobody has verified.
        raise ExportSourceGraphError(
            "graph carries a replay progress record -- its replay from the mutation log "
            "is unfinished or failed, so it is not exported"
        )

    count_rows = _rows(graph, _TOTAL_NODE_COUNT_QUERY)
    total_node_count = cast("int", count_rows[0][0]) if count_rows else 0  # no rows: no graph key
    if len(nodes) != total_node_count:
        raise ExportSourceGraphError(
            f"graph has {total_node_count} node(s) total but only {len(nodes)} were reachable "
            "via a single-label MATCH -- some node carries zero labels"
        )

    edges: list[SerializedEdge] = []
    for relationship_type in _enumerate_relationship_types(graph):
        edges.extend(_read_edges_for_relationship_type(graph, relationship_type))

    domain_nodes = tuple(node for node in nodes if node.label not in MERGE_MARKER_LABELS)
    return SerializedGraph(nodes=domain_nodes, edges=tuple(edges))


def _node_sort_key(node: SerializedNode) -> tuple[str, str]:
    """Order nodes by label, then by domain id (empty string when absent)."""
    return (node.label, cast("str", node.properties.get("id", "")))


def _edge_sort_key(edge: SerializedEdge) -> tuple[str, str, str]:
    """Order edges by relationship type, then source id, then target id."""
    return (edge.relationship_type, edge.source_id, edge.target_id)


def to_json_bytes(graph: SerializedGraph) -> bytes:
    """Encode `graph` as canonical, deterministic JSON bytes.

    These are exactly the bytes written as `baseline.json` or `native.json`, and
    `checksum_bytes` hashes exactly this output. Nodes are sorted by `(label, id)`, edges by
    `(relationship_type, source_id, target_id)`, keys are sorted, non-ASCII text is kept as is,
    the indent is two spaces, and the text ends with a newline, so equal graphs always encode
    to equal bytes.

    Args:
        graph: The serialized graph to encode.

    Returns:
        UTF-8 bytes of the JSON document `{"nodes": [...], "edges": [...]}`.
    """
    document = {
        "nodes": [
            {"label": node.label, "properties": node.properties}
            for node in sorted(graph.nodes, key=_node_sort_key)
        ],
        "edges": [
            {
                "relationship_type": edge.relationship_type,
                "source_label": edge.source_label,
                "source_id": edge.source_id,
                "target_label": edge.target_label,
                "target_id": edge.target_id,
                "properties": edge.properties,
            }
            for edge in sorted(graph.edges, key=_edge_sort_key)
        ],
    }
    text = json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2)
    return (text + "\n").encode("utf-8")


def parse_serialized_graph_json(blob: bytes) -> SerializedGraph:
    """Decode bytes produced by `to_json_bytes` back into a `SerializedGraph`.

    Restore calls this after checksum verification, so a corrupted or tampered blob never
    reaches `json.loads`. The document shape is trusted, not validated.

    Args:
        blob: JSON bytes in the `to_json_bytes` format.

    Returns:
        The decoded graph, with nodes and edges in document order.

    Raises:
        json.JSONDecodeError: `blob` is not valid JSON.
        KeyError: A required key (`nodes`, `edges`, or an item field) is missing.
    """
    document = cast("dict[str, object]", json.loads(blob))
    nodes = tuple(
        SerializedNode(
            label=cast("str", node["label"]),
            properties=cast("dict[str, SerializedPropertyValue]", node["properties"]),
        )
        for node in cast("list[dict[str, object]]", document["nodes"])
    )
    edges = tuple(
        SerializedEdge(
            relationship_type=cast("str", edge["relationship_type"]),
            source_label=cast("str", edge["source_label"]),
            source_id=cast("str", edge["source_id"]),
            target_label=cast("str", edge["target_label"]),
            target_id=cast("str", edge["target_id"]),
            properties=cast("dict[str, SerializedPropertyValue]", edge["properties"]),
        )
        for edge in cast("list[dict[str, object]]", document["edges"])
    )
    return SerializedGraph(nodes=nodes, edges=edges)
