"""Tests for `ps_service.change_monitor.succession` (PLAN_REVIEWED.md §3 test 12).

The fused succession write's exact Cypher / params, the deterministic prior
lookup (0 / >1 rows -> `ChangeMonitorStateError`), the `new_node_exists` /
`is_succession_complete` truth tables, the `RedisError` ->
`SuccessionPersistenceError` + `mark_unhealthy` path, and idempotent re-run.
"""

from __future__ import annotations

import pytest

from change_monitor._fakes import FakeGraph, FakeQueryResult, RaisingGraph
from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    SuccessionPersistenceError,
)
from ps_service.change_monitor.models import PriorInstrument
from ps_service.change_monitor.succession import (
    find_prior_instrument,
    is_succession_complete,
    link_and_supersede,
    new_node_exists,
    set_new_version_property,
)
from ps_service.dependency_health import FALKORDB, is_healthy

_FIND_PRIOR_QUERY = """\
MATCH (n:RegulatoryInstrument)
WHERE n.status = 'active'
  AND n.id <> $new_id
  AND NOT (n)-[:SUPERSEDED_BY]->(:RegulatoryInstrument {id: $new_id})
RETURN n.id AS id, n.instrument_type AS instrument_type"""

_NEW_NODE_EXISTS_QUERY = "MATCH (n:RegulatoryInstrument {id: $new_id}) RETURN n.status AS status"

_SUCCESSION_COMPLETE_QUERY = """\
MATCH (prior:RegulatoryInstrument {status: 'superseded'})-[:SUPERSEDED_BY]->
      (new:RegulatoryInstrument {id: $new_id})
RETURN prior.id AS prior_id"""

_SET_VERSION_QUERY = "MATCH (n:RegulatoryInstrument {id: $new_id}) SET n.version = $new_version"

_FUSED_QUERY = """\
MATCH (prior:RegulatoryInstrument {id: $prior_id}),
      (new:RegulatoryInstrument {id: $new_id})
MERGE (prior)-[:SUPERSEDED_BY]->(new)
SET prior.status = 'superseded'"""


# --- find_prior_instrument ------------------------------------------------


def test_find_prior_instrument_issues_the_deterministic_query() -> None:
    graph = FakeGraph([FakeQueryResult([["CRA-1.0", "regulation"]])])

    prior = find_prior_instrument(graph, "CRA-2.0")

    assert prior == PriorInstrument(id="CRA-1.0", instrument_type="regulation")
    assert len(graph.calls) == 1
    assert graph.calls[0].query == _FIND_PRIOR_QUERY
    assert graph.calls[0].params == {"new_id": "CRA-2.0"}


def test_find_prior_instrument_raises_when_no_active_prior() -> None:
    graph = FakeGraph([FakeQueryResult([])])

    with pytest.raises(ChangeMonitorStateError):
        find_prior_instrument(graph, "CRA-2.0")


def test_find_prior_instrument_raises_when_more_than_one_active_prior() -> None:
    graph = FakeGraph([FakeQueryResult([["CRA-1.0", "regulation"], ["CRA-1.5", "regulation"]])])

    with pytest.raises(ChangeMonitorStateError, match=r"CRA-1\.0"):
        find_prior_instrument(graph, "CRA-2.0")


# --- new_node_exists / is_succession_complete truth tables ----------------


def test_new_node_exists_returns_status_when_the_node_is_present() -> None:
    graph = FakeGraph([FakeQueryResult([["active"]])])

    assert new_node_exists(graph, "CRA-2.0") == "active"
    assert graph.calls[0].query == _NEW_NODE_EXISTS_QUERY
    assert graph.calls[0].params == {"new_id": "CRA-2.0"}


def test_new_node_exists_returns_none_when_the_node_is_absent() -> None:
    graph = FakeGraph([FakeQueryResult([])])

    assert new_node_exists(graph, "CRA-2.0") is None


def test_is_succession_complete_returns_prior_id_when_the_edge_is_present() -> None:
    graph = FakeGraph([FakeQueryResult([["CRA-1.0"]])])

    assert is_succession_complete(graph, "CRA-2.0") == "CRA-1.0"
    assert graph.calls[0].query == _SUCCESSION_COMPLETE_QUERY
    assert graph.calls[0].params == {"new_id": "CRA-2.0"}


def test_is_succession_complete_returns_none_when_no_completed_edge() -> None:
    graph = FakeGraph([FakeQueryResult([])])

    assert is_succession_complete(graph, "CRA-2.0") is None


# --- set_new_version_property --------------------------------------------


def test_set_new_version_property_issues_the_exact_set_query() -> None:
    graph = FakeGraph()

    set_new_version_property(graph, "CRA-2.0", "2.0")

    assert len(graph.calls) == 1
    assert graph.calls[0].query == _SET_VERSION_QUERY
    assert graph.calls[0].params == {"new_id": "CRA-2.0", "new_version": "2.0"}


# --- link_and_supersede: the single fused statement ---------------------


def test_link_and_supersede_issues_the_single_fused_statement() -> None:
    graph = FakeGraph()

    link_and_supersede(graph, "CRA-1.0", "CRA-2.0")

    assert len(graph.calls) == 1
    assert graph.calls[0].query == _FUSED_QUERY
    assert graph.calls[0].params == {"prior_id": "CRA-1.0", "new_id": "CRA-2.0"}


def test_link_and_supersede_is_idempotent_on_re_run() -> None:
    graph = FakeGraph()

    link_and_supersede(graph, "CRA-1.0", "CRA-2.0")
    link_and_supersede(graph, "CRA-1.0", "CRA-2.0")

    assert [call.query for call in graph.calls] == [_FUSED_QUERY, _FUSED_QUERY]


# --- RedisError -> SuccessionPersistenceError + mark_unhealthy ----------


def test_write_wraps_redis_error_and_marks_falkordb_unhealthy() -> None:
    graph = RaisingGraph()

    with pytest.raises(SuccessionPersistenceError):
        link_and_supersede(graph, "CRA-1.0", "CRA-2.0")

    assert is_healthy(FALKORDB) is False


def test_read_wraps_redis_error_and_marks_falkordb_unhealthy() -> None:
    graph = RaisingGraph()

    with pytest.raises(SuccessionPersistenceError):
        new_node_exists(graph, "CRA-2.0")

    assert is_healthy(FALKORDB) is False


def test_successful_write_marks_falkordb_healthy() -> None:
    graph = FakeGraph()

    set_new_version_property(graph, "CRA-2.0", "2.0")

    assert is_healthy(FALKORDB) is True
