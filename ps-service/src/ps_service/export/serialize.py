"""Serialization/checksum primitives for `ps_service.export`.

PLAN.md D9, CHANGES2.md §2 -- replaces `dump.py`'s `DUMP`-based design
entirely. `serialize_graph` reads a whole FalkorDB graph into a `SerializedGraph`
generically -- via `CALL db.labels()`/`CALL db.relationshipTypes()`
enumeration plus one `MATCH` per label/relationship type -- rather than
hardcoding a fixed node/edge catalog, so the same function is correct for
both the baseline graph and the native structural graph (CHANGES2.md §2.3).
`to_json_bytes`/`parse_serialized_graph_json` are the deterministic
JSON <-> `SerializedGraph` codec (§2.1); `checksum_bytes` is unchanged from
the old `dump.py` (still a pure `hashlib.sha256` wrapper, D9).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol, cast

from ps_service.export.errors import ExportSourceGraphError
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode

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
    """SHA-256 hex digest of `blob` (D9) -- a manifest's `baseline_sha256`/`native_sha256` value."""
    return hashlib.sha256(blob).hexdigest()


class _QueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult` -- the one field this module reads."""

    @property
    def result_set(self) -> list[object]: ...


def _rows(
    graph: _GraphQueryHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    """The one call site every `graph.query()` read in this module goes through."""
    result = graph.query(query, params)
    return cast("list[list[object]]", cast("_QueryResult", result).result_set)


def _enumerate_labels(graph: _GraphQueryHandle) -> list[str]:
    return [cast("str", row[0]) for row in _rows(graph, "CALL db.labels()")]


def _enumerate_relationship_types(graph: _GraphQueryHandle) -> list[str]:
    return [cast("str", row[0]) for row in _rows(graph, "CALL db.relationshipTypes()")]


def _properties(raw: object) -> dict[str, SerializedPropertyValue]:
    return cast("dict[str, SerializedPropertyValue]", dict(cast("dict[str, object]", raw)))


def _read_nodes_for_label(graph: _GraphQueryHandle, label: str) -> list[SerializedNode]:
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
    """Read every node/edge out of an already-selected graph, generically.

    Caller has already selected `graph` (mirrors `company_merge.graph_reader.
    read_baseline_graph`'s own "caller selects, this function doesn't"
    convention exactly). Enumerates the graph's OWN labels/relationship
    types via `CALL db.labels()`/`CALL db.relationshipTypes()` rather than
    hardcoding Role/Requirement/Obligation/Capability -- this is what makes
    ONE function correct for both the baseline graph (RegulatoryInstrument/
    Role/Requirement/Obligation/Capability[/Policy/Standard/Control], D15)
    and the native structural graph (RegulatoryInstrument plus dynamic
    per-element-type labels, `ingestion/graph_writer.py`'s
    `_KNOWN_ELEMENT_TYPES`) with no graph-kind parameter. One query per
    label for nodes, one per relationship type for edges -- empirically
    confirmed both correct and cheap (a handful of queries per graph, not
    one per node/edge; CHANGES2.md §4).

    Raises `ExportSourceGraphError` if any node, or any edge endpoint,
    carries zero or more than one label.
    """
    nodes: list[SerializedNode] = []
    for label in _enumerate_labels(graph):
        nodes.extend(_read_nodes_for_label(graph, label))

    total_node_count = cast("int", _rows(graph, _TOTAL_NODE_COUNT_QUERY)[0][0])
    if len(nodes) != total_node_count:
        raise ExportSourceGraphError(
            f"graph has {total_node_count} node(s) total but only {len(nodes)} were reachable "
            "via a single-label MATCH -- some node carries zero labels"
        )

    edges: list[SerializedEdge] = []
    for relationship_type in _enumerate_relationship_types(graph):
        edges.extend(_read_edges_for_relationship_type(graph, relationship_type))

    return SerializedGraph(nodes=tuple(nodes), edges=tuple(edges))


def _node_sort_key(node: SerializedNode) -> tuple[str, str]:
    return (node.label, cast("str", node.properties.get("id", "")))


def _edge_sort_key(edge: SerializedEdge) -> tuple[str, str, str]:
    return (edge.relationship_type, edge.source_id, edge.target_id)


def to_json_bytes(graph: SerializedGraph) -> bytes:
    """Canonical, deterministic encoding of `graph`.

    Nodes sorted by (label, id), edges sorted by (relationship_type,
    source_id, target_id), `json.dumps(..., sort_keys=True, ensure_ascii=
    False, indent=2)` + trailing newline. THIS is what lands as
    baseline.json/native.json's bytes -- checksum_bytes hashes exactly this
    output, nothing else.
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
    """Inverse of `to_json_bytes`.

    Restore calls this AFTER checksum verification (D9) -- a corrupted/
    tampered blob never reaches `json.loads`.
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
