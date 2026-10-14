"""Company Merge reads through the gateway's `ReadOnlyGraph` and imports no driver."""

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

import ps_service.company_merge as company_merge_package
from ps_service.company_merge import dedup, falkordb_client, graph_writer, pending_review
from ps_service.config import load_config

_PACKAGE_DIR = Path(company_merge_package.__file__).parent


def test_no_company_merge_module_imports_the_driver_or_builds_a_client() -> None:
    assert driver_findings(_PACKAGE_DIR) == []


def test_the_public_names_are_still_there() -> None:
    for name in (
        "GraphHandle",
        "GraphQueryResult",
        "check_connectivity",
        "connect",
        "connect_from_config",
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


def test_the_readers_return_empty_or_zero_on_a_missing_graph_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_driver(monkeypatch, EmptyKeyDriverGraph())
    database = falkordb_client.connect_from_config(load_config())
    graph = falkordb_client.select_graph(database, "policy_system")

    assert pending_review.list_pending_reviews(graph) == ()
    assert pending_review.get_pending_review(graph, "no-such-review") is None
    assert dedup.read_existing_canonical_index(graph, "Capability") == ()
    assert graph_writer.count_new_obligations(graph, ("a", "b", "a")) == 2


@pytest.mark.falkordb_live
def test_the_readers_return_empty_or_zero_on_a_graph_key_that_does_not_exist_live() -> None:
    database = falkordb_client.connect_from_config(load_config())
    name = f"s3a_never_created_{uuid.uuid4().hex}"
    graph = falkordb_client.select_graph(database, name)

    assert pending_review.list_pending_reviews(graph) == ()
    assert graph_writer.count_new_obligations(graph, ("a", "b")) == 2
    assert name not in database.list_graphs()
