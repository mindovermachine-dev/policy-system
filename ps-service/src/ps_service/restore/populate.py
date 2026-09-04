"""Batched, parameterized graph-population primitive for `ps_service.restore`.

CHANGES2.md §2.3 -- replaces the old `stage_dump`'s raw `RESTORE` with
generic, batched `UNWIND`+`CREATE`/`MERGE` writes built from an already-
parsed `SerializedGraph`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode


def _nodes_by_label(nodes: tuple[SerializedNode, ...]) -> dict[str, list[SerializedNode]]:
    grouped: dict[str, list[SerializedNode]] = {}
    for node in nodes:
        grouped.setdefault(node.label, []).append(node)
    return grouped


def _edges_by_triple(
    edges: tuple[SerializedEdge, ...],
) -> dict[tuple[str, str, str], list[SerializedEdge]]:
    grouped: dict[tuple[str, str, str], list[SerializedEdge]] = {}
    for edge in edges:
        key = (edge.relationship_type, edge.source_label, edge.target_label)
        grouped.setdefault(key, []).append(edge)
    return grouped


def _create_nodes(graph: _GraphQueryHandle, label: str, nodes: list[SerializedNode]) -> None:
    rows = [dict(node.properties) for node in nodes]
    graph.query(f"UNWIND $rows AS row CREATE (n:{label}) SET n = row", {"rows": rows})


def _merge_edges(
    graph: _GraphQueryHandle,
    relationship_type: str,
    source_label: str,
    target_label: str,
    edges: list[SerializedEdge],
) -> None:
    rows = [
        {
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            "properties": dict(edge.properties),
        }
        for edge in edges
    ]
    query = (
        f"UNWIND $rows AS row "
        f"MATCH (s:{source_label} {{id: row.source_id}}), (t:{target_label} {{id: row.target_id}}) "
        f"MERGE (s)-[r:{relationship_type}]->(t) SET r = row.properties"
    )
    graph.query(query, {"rows": rows})


def populate_graph(graph: _GraphQueryHandle, serialized: SerializedGraph) -> None:
    """Build `graph` from `serialized`'s nodes/edges via batched, parameterized writes.

    `graph` is an already-selected, freshly-created staged key -- NEVER the
    live target. Caller MUST call `schema_allowlist.validate_serialized_graph`
    first -- this function does no validation of its own (mirrors
    `ingestion/domain_mapper`'s "validate-then-write, never re-check"
    convention).

    One `UNWIND $rows AS row CREATE (n:{label}) SET n = row` per distinct
    label (batched, not one CREATE per node); one `UNWIND $rows AS row
    MATCH (s:{source_label} {id: row.source_id}), (t:{target_label}
    {id: row.target_id}) MERGE (s)-[r:{type}]->(t) SET r = row.properties`
    per distinct (relationship_type, source_label, target_label) triple --
    matching on each endpoint's stable `id` property, never FalkorDB's
    internal node id. Property values only ever flow through `$rows`
    parameters, never interpolated -- only the (already allow-listed)
    label/relationship_type strings are f-string-interpolated, matching
    every existing writer's Query Safety pattern in this codebase exactly.
    """
    for label, nodes in _nodes_by_label(serialized.nodes).items():
        _create_nodes(graph, label, nodes)
    for (relationship_type, source_label, target_label), edges in _edges_by_triple(
        serialized.edges
    ).items():
        _merge_edges(graph, relationship_type, source_label, target_label, edges)
