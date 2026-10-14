"""Change Monitor reads through the gateway's `ReadOnlyGraph` and imports no driver."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from graph_gateway.read_only_client_checks import (
    EmptyKeyDriverGraph,
    RecordingDriverGraph,
    driver_findings,
    install_driver,
)

import ps_service.change_monitor as change_monitor_package
from ps_service.change_monitor import falkordb_client, graph_reader, succession
from ps_service.config import load_config

_PACKAGE_DIR = Path(change_monitor_package.__file__).parent


def test_no_change_monitor_module_imports_the_driver_or_builds_a_client() -> None:
    assert driver_findings(_PACKAGE_DIR) == []


def test_the_public_names_are_still_there() -> None:
    for name in (
        "GraphHandle",
        "GraphQueryResult",
        "check_connectivity",
        "connect",
        "connect_from_config",
        "native_graph_name",
        "select_graph",
        "single_tenant_graph_name",
    ):
        assert hasattr(falkordb_client, name), name


def test_a_graph_opened_from_config_sends_only_ro_query_to_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = RecordingDriverGraph()
    install_driver(monkeypatch, graph)
    database = falkordb_client.connect_from_config(load_config())

    falkordb_client.select_graph(database, "policy_system").query("MATCH (n) RETURN n")

    assert graph.methods == ["ro_query"]


def test_the_readers_return_empty_on_a_missing_graph_key(monkeypatch: pytest.MonkeyPatch) -> None:
    install_driver(monkeypatch, EmptyKeyDriverGraph())
    database = falkordb_client.connect_from_config(load_config())
    graph = falkordb_client.select_graph(database, "policy_system")

    assert graph_reader.read_tracked_instruments(graph) == ()
    assert succession.new_node_exists(graph, "CRA-2.0") is None
    assert succession.is_succession_complete(graph, "CRA-2.0") is None
    assert succession.read_reingestion_facts(graph, "CRA-2.0").node_exists is False


@pytest.mark.falkordb_live
def test_the_readers_return_empty_on_a_graph_key_that_does_not_exist_live() -> None:
    database = falkordb_client.connect_from_config(load_config())
    name = f"s3d_never_created_{uuid.uuid4().hex}"
    graph = falkordb_client.select_graph(database, name)

    assert graph_reader.read_tracked_instruments(graph) == ()
    assert succession.new_node_exists(graph, "CRA-2.0") is None
    assert name not in database.list_graphs()
