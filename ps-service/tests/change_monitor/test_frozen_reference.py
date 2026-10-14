"""The frozen legacy reference is what the pre-gateway succession writer was.

The equivalence tests compare the gateway-backed succession against these statements, so the
statements must be the original ones and the interpreter must model only what it was taught.
"""

from __future__ import annotations

import hashlib

import pytest

from change_monitor._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    FROZEN_STATEMENTS,
    LegacyRecordingGraph,
    legacy_clear_marker,
    legacy_link_and_supersede,
    legacy_mark_stage,
    legacy_set_version,
    legacy_supersede_in_single_tenant,
)

_PRIOR = "CRA-1.0"
_NEW = "CRA-2.0"


def _graph(*, with_new: bool = True, with_prior: bool = True) -> LegacyRecordingGraph:
    graph = LegacyRecordingGraph()
    if with_prior:
        graph.nodes[("RegulatoryInstrument", _PRIOR)] = {"status": "active"}
    if with_new:
        graph.nodes[("RegulatoryInstrument", _NEW)] = {"status": "active", "version": "1.0"}
    return graph


def test_frozen_statements_match_their_pinned_hashes() -> None:
    assert set(FROZEN_STATEMENTS) == set(FROZEN_STATEMENT_HASHES)
    for name, text in FROZEN_STATEMENTS.items():
        assert hashlib.sha256(text.encode()).hexdigest() == FROZEN_STATEMENT_HASHES[name], name


def test_the_interpreter_rejects_a_statement_shape_it_was_not_taught() -> None:
    graph = LegacyRecordingGraph()

    with pytest.raises(AssertionError, match="not a frozen legacy statement"):
        graph.query("MATCH (n) DETACH DELETE n", {"x": 1})
    assert graph.statements == []


def test_set_version_sets_the_property_on_the_new_node_only() -> None:
    graph = _graph()

    legacy_set_version(graph, _NEW, "2.0")

    assert graph.nodes[("RegulatoryInstrument", _NEW)]["version"] == "2.0"
    assert "version" not in graph.nodes[("RegulatoryInstrument", _PRIOR)]


def test_set_version_on_a_missing_node_is_a_silent_no_op() -> None:
    graph = _graph(with_new=False)

    legacy_set_version(graph, _NEW, "2.0")

    assert set(graph.nodes) == {("RegulatoryInstrument", _PRIOR)}


def test_mark_stage_creates_then_updates_one_marker() -> None:
    graph = _graph()

    legacy_mark_stage(graph, _NEW, "ingestion")
    legacy_mark_stage(graph, _NEW, "extraction")

    markers = {key: props for key, props in graph.nodes.items() if key[0] == "ReingestProgress"}
    assert markers == {("ReingestProgress", _NEW): {"stage": "extraction"}}


def test_the_fused_link_writes_edge_absorbed_status_and_marker_together() -> None:
    graph = _graph()

    legacy_link_and_supersede(graph, _PRIOR, _NEW)

    edge = ("SUPERSEDED_BY", "RegulatoryInstrument", _PRIOR, "RegulatoryInstrument", _NEW)
    assert graph.edges == {edge: {"absorbed": True}}
    assert graph.nodes[("RegulatoryInstrument", _PRIOR)]["status"] == "superseded"
    assert graph.nodes[("ReingestProgress", _NEW)] == {"stage": "linked"}


@pytest.mark.parametrize(("with_prior", "with_new"), [(False, True), (True, False)])
def test_the_fused_link_writes_nothing_when_a_node_is_missing(
    *, with_prior: bool, with_new: bool
) -> None:
    graph = _graph(with_prior=with_prior, with_new=with_new)
    before = {key: dict(props) for key, props in graph.nodes.items()}

    legacy_link_and_supersede(graph, _PRIOR, _NEW)

    assert graph.edges == {}
    assert graph.nodes == before


def test_the_fused_link_is_idempotent() -> None:
    graph = _graph()
    legacy_link_and_supersede(graph, _PRIOR, _NEW)
    once = (
        {k: dict(v) for k, v in graph.nodes.items()},
        {k: dict(v) for k, v in graph.edges.items()},
    )

    legacy_link_and_supersede(graph, _PRIOR, _NEW)

    assert (graph.nodes, graph.edges) == once


def test_the_single_tenant_supersede_adds_the_edge_and_the_status_and_reports_a_match() -> None:
    graph = _graph()

    matched = legacy_supersede_in_single_tenant(graph, _PRIOR, _NEW)

    assert matched is True
    edge = ("SUPERSEDED_BY", "RegulatoryInstrument", _PRIOR, "RegulatoryInstrument", _NEW)
    assert graph.edges == {edge: {}}
    assert graph.nodes[("RegulatoryInstrument", _PRIOR)]["status"] == "superseded"


def test_the_single_tenant_supersede_reports_no_match_and_writes_nothing_for_a_missing_node() -> (
    None
):
    graph = _graph(with_new=False)

    matched = legacy_supersede_in_single_tenant(graph, _PRIOR, _NEW)

    assert matched is False
    assert graph.edges == {}
    assert graph.nodes[("RegulatoryInstrument", _PRIOR)]["status"] == "active"


def test_clear_marker_deletes_the_marker_and_is_a_no_op_without_one() -> None:
    graph = _graph()
    legacy_mark_stage(graph, _NEW, "merge")

    legacy_clear_marker(graph, _NEW)
    legacy_clear_marker(graph, _NEW)

    assert ("ReingestProgress", _NEW) not in graph.nodes


def test_as_gateway_graph_stamps_every_edge_with_the_shared_identity_formula() -> None:
    graph = _graph()
    legacy_link_and_supersede(graph, _PRIOR, _NEW)

    stamped = graph.as_gateway_graph()

    assert [key[5] for key in stamped.edges] == [f"SUPERSEDED_BY:{_PRIOR}->{_NEW}"]
