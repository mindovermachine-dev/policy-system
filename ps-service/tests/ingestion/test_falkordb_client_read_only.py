"""Ingestion reads through the gateway's `ReadOnlyGraph` and imports no driver."""

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

import ps_service.ingestion as ingestion_package
from ps_service.config import load_config
from ps_service.ingestion import falkordb_client, graph_writer

_PACKAGE_DIR = Path(ingestion_package.__file__).parent


def test_no_ingestion_module_imports_the_driver_or_the_write_capable_factory() -> None:
    assert driver_findings(_PACKAGE_DIR) == []


def test_the_public_names_are_still_there_and_the_driver_class_is_gone() -> None:
    for name in (
        "FalkorDBConnectionError",
        "GraphHandle",
        "GraphQueryResult",
        "check_connectivity",
        "check_connectivity_from_config",
        "connect",
        "connect_from_config",
        "native_graph_name",
        "select_graph",
    ):
        assert hasattr(falkordb_client, name), name
    assert not hasattr(falkordb_client, "FalkorDB")


def test_a_graph_opened_from_config_sends_only_ro_query_to_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = RecordingDriverGraph()
    install_driver(monkeypatch, graph)
    database = falkordb_client.connect_from_config(load_config())

    falkordb_client.select_graph(database, "cra_native").query("MATCH (n) RETURN n")

    assert graph.methods == ["ro_query"]


def test_the_reachability_read_returns_zero_counts_on_a_missing_graph_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_driver(monkeypatch, EmptyKeyDriverGraph())
    database = falkordb_client.connect_from_config(load_config())
    graph = falkordb_client.select_graph(database, "cra_native")

    counts = graph_writer.verify_structural_graph_reachable(graph, "CRA-1.0")

    assert {count.total for count in counts.values()} == {0}
    assert {count.reachable for count in counts.values()} == {0}


@pytest.mark.falkordb_live
def test_the_reachability_read_returns_zero_counts_on_a_graph_key_that_does_not_exist_live() -> (
    None
):
    database = falkordb_client.connect_from_config(load_config())
    name = f"s3b_never_created_{uuid.uuid4().hex}"
    graph = falkordb_client.select_graph(database, name)

    counts = graph_writer.verify_structural_graph_reachable(graph, "CRA-1.0")

    assert {count.total for count in counts.values()} == {0}
    assert name not in database.list_graphs()
