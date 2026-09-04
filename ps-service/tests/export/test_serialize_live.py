"""Live FalkorDB proof for `ps_service.export.serialize.serialize_graph` (CHANGES2.md §3.4).

Supersedes `test_dump_live.py` (PLAN.md Slice 2.3, tested `dump_graph`,
which no longer exists).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from ps_service.export.errors import ExportSourceGraphError
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.serialize import serialize_graph

if TYPE_CHECKING:
    from falkordb import FalkorDB


@pytest.mark.falkordb_live
def test_serialize_graph_reads_a_real_graphs_nodes_and_edges(live_falkordb: FalkorDB) -> None:
    graph_name = f"__ac66_slice23_serialize_{uuid.uuid4().hex}__"
    live_falkordb.select_graph(graph_name).query(
        "CREATE (a:Test {id: 'x', name: 'y'})-[:LINKS_TO {n: 1}]->(b:Test {id: 'z'})"
    )

    try:
        result = serialize_graph(graph_query_handle(live_falkordb, graph_name))

        assert len(result.nodes) == 2
        assert {node.label for node in result.nodes} == {"Test"}
        assert {str(node.properties["id"]) for node in result.nodes} == {"x", "z"}
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.relationship_type == "LINKS_TO"
        assert edge.source_label == "Test"
        assert edge.source_id == "x"
        assert edge.target_label == "Test"
        assert edge.target_id == "z"
        assert edge.properties == {"n": 1}
    finally:
        live_falkordb.connection.delete(graph_name)
        assert live_falkordb.connection.exists(graph_name) == 0


@pytest.mark.falkordb_live
def test_serialize_graph_raises_on_a_multi_labeled_node(live_falkordb: FalkorDB) -> None:
    graph_name = f"__ac66_slice23_multilabel_{uuid.uuid4().hex}__"
    live_falkordb.select_graph(graph_name).query("CREATE (n:A:B {id: 'x'})")

    try:
        with pytest.raises(ExportSourceGraphError):
            serialize_graph(graph_query_handle(live_falkordb, graph_name))
    finally:
        live_falkordb.connection.delete(graph_name)
        assert live_falkordb.connection.exists(graph_name) == 0
