"""The Domain Mapper reads through `ReadOnlyGraph` and imports no driver."""

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

import ps_service.domain_mapper as domain_mapper_package
from ps_service.config import load_config
from ps_service.domain_mapper import derivation, falkordb_client

_PACKAGE_DIR = Path(domain_mapper_package.__file__).parent


def test_no_domain_mapper_module_imports_the_driver_or_builds_a_client() -> None:
    assert driver_findings(_PACKAGE_DIR) == []


def test_the_public_names_are_still_there() -> None:
    for name in (
        "GraphHandle",
        "GraphQueryResult",
        "baseline_graph_name",
        "check_connectivity",
        "connect",
        "connect_from_config",
        "native_graph_name",
        "select_graph",
    ):
        assert hasattr(falkordb_client, name), name


def test_a_graph_opened_from_config_sends_only_ro_query_to_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = RecordingDriverGraph()
    install_driver(monkeypatch, graph)
    database = falkordb_client.connect_from_config(load_config())

    falkordb_client.select_graph(database, "cra_baseline").query("MATCH (n) RETURN n")

    assert graph.methods == ["ro_query"]


def test_the_requirements_read_returns_nothing_on_a_missing_graph_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_driver(monkeypatch, EmptyKeyDriverGraph())
    database = falkordb_client.connect_from_config(load_config())
    graph = falkordb_client.select_graph(database, "cra_baseline")

    assert derivation._read_requirements_by_role(graph, "CRA-1.0") == {}  # pyright: ignore[reportPrivateUsage]  # the stage seam needs an LLM; this is its graph read


@pytest.mark.falkordb_live
def test_the_requirements_read_returns_nothing_on_a_graph_key_that_does_not_exist_live() -> None:
    database = falkordb_client.connect_from_config(load_config())
    name = f"s3c_never_created_{uuid.uuid4().hex}"
    graph = falkordb_client.select_graph(database, name)

    assert derivation._read_requirements_by_role(graph, "CRA-1.0") == {}  # pyright: ignore[reportPrivateUsage]  # the stage seam needs an LLM; this is its graph read
    assert name not in database.list_graphs()
