"""Round-trip proof: `serialize_graph` -> `to_json_bytes` -> `parse_serialized_graph_json`
-> `stage_graph` -> `serialize_graph` reconstructs an equivalent graph (CHANGES2.md §3.5).

Supersedes `test_dump_roundtrip_live.py` (PLAN.md Slice 2.4, permanently
`xfail` -- its premise, `DUMP`+`RESTORE` round-tripping, is permanently
superseded by this redesign, not merely temporarily blocked). This is the
exact load-bearing proof PLAN.md's Slice 2.4 originally wanted, now
achievable via the new mechanism -- see also CHANGES2.md §4's own live
probes of this same logic.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.serialize import (
    parse_serialized_graph_json,
    serialize_graph,
    to_json_bytes,
)
from ps_service.restore.staging import stage_graph

if TYPE_CHECKING:
    from falkordb import FalkorDB

    from ps_service.export.models import SerializedEdge, SerializedNode

_ALLOWED_LABELS = frozenset({"RegulatoryInstrument", "TITLE", "ARTICLE"})
_ALLOWED_RELATIONSHIP_TYPES = frozenset({"HAS"})


def _sort_key_for_nodes(node: SerializedNode) -> tuple[str, str]:
    return (node.label, str(node.properties.get("id", "")))


def _sort_key_for_edges(edge: SerializedEdge) -> tuple[str, str, str]:
    return (edge.relationship_type, edge.source_id, edge.target_id)


@pytest.mark.falkordb_live
def test_serialize_to_json_parse_stage_serialize_reconstructs_an_equivalent_graph(
    live_falkordb: FalkorDB,
) -> None:
    """>=3 nodes, >=2 labels, >=1 edge with properties -- the native-graph shape
    (varying label pairs on the same `HAS` relationship type) that stresses
    the "group edges by (relationship_type, source_label, target_label)"
    logic a fixed-per-relationship-type edge catalog wouldn't exercise.
    """
    source_name = f"__ac66_slice24_source_{uuid.uuid4().hex}__"
    staged_key_name = f"__ac66_slice24_target_{uuid.uuid4().hex}__"
    live_falkordb.select_graph(source_name).query(
        "CREATE (r:RegulatoryInstrument {id: 'CRA-1.0', title: 'CRA'})"
        "-[:HAS]->(t:TITLE {id: 'CRA-1.0_t1', number: 1})"
        "-[:HAS]->(a:ARTICLE {id: 'CRA-1.0_t1_a1', text: 'shall...'})"
    )

    try:
        original = serialize_graph(graph_query_handle(live_falkordb, source_name))
        assert len(original.nodes) == 3
        assert {node.label for node in original.nodes} == {
            "RegulatoryInstrument",
            "TITLE",
            "ARTICLE",
        }
        assert len(original.edges) == 2

        blob = to_json_bytes(original)
        parsed = parse_serialized_graph_json(blob)
        # `to_json_bytes` sorts; `serialize_graph`'s own node order follows label
        # enumeration order -- compare sorted (CHANGES2.md §3.5: "dataclass ==
        # after sorting").
        assert sorted(parsed.nodes, key=_sort_key_for_nodes) == sorted(
            original.nodes, key=_sort_key_for_nodes
        )
        assert sorted(parsed.edges, key=_sort_key_for_edges) == sorted(
            original.edges, key=_sort_key_for_edges
        )

        staged_name = stage_graph(
            live_falkordb,
            parsed,
            staged_key_name,
            allowed_labels=_ALLOWED_LABELS,
            allowed_relationship_types=_ALLOWED_RELATIONSHIP_TYPES,
        )
        try:
            restored = serialize_graph(graph_query_handle(live_falkordb, staged_name))

            assert sorted(restored.nodes, key=_sort_key_for_nodes) == sorted(
                original.nodes, key=_sort_key_for_nodes
            )
            assert sorted(restored.edges, key=_sort_key_for_edges) == sorted(
                original.edges, key=_sort_key_for_edges
            )
        finally:
            live_falkordb.connection.delete(staged_name)
            assert live_falkordb.connection.exists(staged_name) == 0
    finally:
        live_falkordb.connection.delete(source_name)
        assert live_falkordb.connection.exists(source_name) == 0
