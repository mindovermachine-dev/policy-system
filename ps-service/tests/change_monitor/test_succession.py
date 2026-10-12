"""Tests for `ps_service.change_monitor.succession` (PLAN_REVIEWED.md §3 test 12).

The deterministic prior lookup (0 / >1 rows -> `ChangeMonitorStateError`), the
`new_node_exists` / `is_succession_complete` truth tables and the `RedisError` ->
`SuccessionPersistenceError` + `mark_unhealthy` path of the reads. The succession's WRITES are
gateway groups (issue #213), tested in `test_succession_gateway.py`.
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
    ReingestionFacts,
    find_prior_instrument,
    is_succession_complete,
    new_node_exists,
    read_reingestion_facts,
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

_FACTS_QUERY = """\
MATCH (n:RegulatoryInstrument {id: $new_id})
OPTIONAL MATCH (p:RegulatoryInstrument)-[e:SUPERSEDED_BY]->(n)
OPTIONAL MATCH (m:ReingestProgress {id: $new_id})
RETURN p.id AS prior_id, p.instrument_type AS prior_instrument_type,
       p.status AS prior_status, e.absorbed AS absorbed, m.stage AS stage"""


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


# --- read_reingestion_facts (Q1) ----------------------------------------


def test_read_reingestion_facts_zero_rows_means_the_node_is_absent() -> None:
    graph = FakeGraph([FakeQueryResult([])])

    facts = read_reingestion_facts(graph, "CRA-2.0")

    assert facts == ReingestionFacts(
        node_exists=False,
        prior_id=None,
        prior_instrument_type=None,
        prior_status=None,
        absorbed=None,
        marker_stage=None,
    )
    assert graph.calls[0].query == _FACTS_QUERY
    assert graph.calls[0].params == {"new_id": "CRA-2.0"}


def test_read_reingestion_facts_reads_edge_absorbed_and_marker() -> None:
    graph = FakeGraph([FakeQueryResult([["CRA-1.0", "regulation", "superseded", True, "linked"]])])

    facts = read_reingestion_facts(graph, "CRA-2.0")

    assert facts == ReingestionFacts(
        node_exists=True,
        prior_id="CRA-1.0",
        prior_instrument_type="regulation",
        prior_status="superseded",
        absorbed=True,
        marker_stage="linked",
    )


def test_read_reingestion_facts_node_without_edge_has_no_prior() -> None:
    graph = FakeGraph([FakeQueryResult([[None, None, None, None, None]])])

    facts = read_reingestion_facts(graph, "CRA-2.0")

    assert facts.node_exists is True
    assert facts.prior_id is None
    assert facts.marker_stage is None


def test_read_reingestion_facts_rejects_two_superseding_priors() -> None:
    graph = FakeGraph(
        [
            FakeQueryResult(
                [
                    ["CRA-1.0", "regulation", "superseded", True, None],
                    ["CRA-1.5", "regulation", "superseded", True, None],
                ]
            )
        ]
    )

    with pytest.raises(ChangeMonitorStateError, match=r"CRA-1\.0"):
        read_reingestion_facts(graph, "CRA-2.0")


# --- RedisError -> SuccessionPersistenceError + mark_unhealthy ----------


def test_read_wraps_redis_error_and_marks_falkordb_unhealthy() -> None:
    graph = RaisingGraph()

    with pytest.raises(SuccessionPersistenceError):
        new_node_exists(graph, "CRA-2.0")

    assert is_healthy(FALKORDB) is False
