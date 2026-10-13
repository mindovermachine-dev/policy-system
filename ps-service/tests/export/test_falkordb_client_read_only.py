"""Export reads through the gateway's `ReadOnlyGraph` and imports no driver (#215, S3e)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from graph_gateway.read_only_client_checks import (
    EmptyKeyDriverGraph,
    RecordingDriverDatabase,
    RecordingDriverGraph,
    driver_findings,
    install_driver,
)

import ps_service.export as export_package
import ps_service.graph_gateway as graph_gateway_package
from ps_service.config import load_config
from ps_service.export import falkordb_connection, serialize
from ps_service.graph_gateway import cypher
from ps_service.graph_gateway.connection import connect_from_config
from ps_service.graph_gateway.read_only import open_read_only_database

_PACKAGE_DIR = Path(export_package.__file__).parent


def test_no_export_module_imports_the_driver_the_key_port_or_the_gateway_cypher() -> None:
    assert driver_findings(_PACKAGE_DIR) == []


def test_the_replay_state_label_is_a_public_gateway_name() -> None:
    assert graph_gateway_package.REPLAY_STATE_LABEL == cypher.REPLAY_STATE_LABEL


def test_the_connection_module_offers_only_the_read_handle() -> None:
    assert hasattr(falkordb_connection, "graph_query_handle")
    for name in ("raw_connection", "graph_copy_handle", "_RawGraphConnection"):
        assert not hasattr(falkordb_connection, name), name


def test_a_query_handle_sends_only_ro_query_to_the_driver() -> None:
    graph = RecordingDriverGraph()
    database = open_read_only_database(
        "h", 1, connect=lambda _host, _port: RecordingDriverDatabase(graph)
    )

    falkordb_connection.graph_query_handle(database, "cra_baseline").query("MATCH (n) RETURN n")

    assert graph.methods == ["ro_query"]


def test_serialize_graph_reads_a_missing_graph_key_as_the_empty_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_driver(monkeypatch, EmptyKeyDriverGraph())
    database = open_read_only_database("h", 1)

    graph = serialize.serialize_graph(
        falkordb_connection.graph_query_handle(database, "cra_native")
    )

    assert (graph.nodes, graph.edges) == ((), ())


@pytest.mark.falkordb_live
def test_serialize_graph_reads_a_graph_key_that_does_not_exist_as_the_empty_graph_live() -> None:
    config = load_config()
    database = open_read_only_database(config.falkordb_host, config.falkordb_port)
    name = f"s3e_never_created_{uuid.uuid4().hex}"

    graph = serialize.serialize_graph(falkordb_connection.graph_query_handle(database, name))

    assert (graph.nodes, graph.edges) == ((), ())
    assert name not in connect_from_config(config).list_graphs()
