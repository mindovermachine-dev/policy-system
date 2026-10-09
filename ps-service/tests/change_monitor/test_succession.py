"""Tests for `ps_service.change_monitor.succession` (PLAN_REVIEWED.md §3 test 12).

The fused succession write's exact Cypher / params, the deterministic prior
lookup (0 / >1 rows -> `ChangeMonitorStateError`), the `new_node_exists` /
`is_succession_complete` truth tables, the `RedisError` ->
`SuccessionPersistenceError` + `mark_unhealthy` path, and idempotent re-run.
"""

from __future__ import annotations

import pytest

from change_monitor._fakes import (
    FakeGraph,
    FakeQueryResult,
    LedgerNativeGraph,
    LedgerSingleTenantGraph,
    RaisingGraph,
)
from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    SuccessionPersistenceError,
)
from ps_service.change_monitor.models import PriorInstrument
from ps_service.change_monitor.succession import (
    ReingestionFacts,
    clear_marker,
    find_prior_instrument,
    is_succession_complete,
    link_and_supersede,
    mark_stage_complete,
    new_node_exists,
    read_reingestion_facts,
    set_new_version_property,
    supersede_in_single_tenant,
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
MERGE (prior)-[e:SUPERSEDED_BY]->(new)
SET e.absorbed = true, prior.status = 'superseded'
WITH new
MERGE (m:ReingestProgress {id: $new_id})
SET m.stage = 'linked'"""

_FACTS_QUERY = """\
MATCH (n:RegulatoryInstrument {id: $new_id})
OPTIONAL MATCH (p:RegulatoryInstrument)-[e:SUPERSEDED_BY]->(n)
OPTIONAL MATCH (m:ReingestProgress {id: $new_id})
RETURN p.id AS prior_id, p.instrument_type AS prior_instrument_type,
       p.status AS prior_status, e.absorbed AS absorbed, m.stage AS stage"""

_MARK_QUERY = """\
MERGE (m:ReingestProgress {id: $new_id})
SET m.stage = $stage"""

_CLEAR_QUERY = "MATCH (m:ReingestProgress {id: $new_id}) DELETE m"


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


def test_link_and_supersede_sets_edge_status_absorbed_and_marker_linked() -> None:
    graph = LedgerNativeGraph()
    graph.seed_instrument("CRA-1.0")
    graph.seed_instrument("CRA-2.0")

    link_and_supersede(graph, "CRA-1.0", "CRA-2.0")

    assert graph.edges == {("SUPERSEDED_BY", "CRA-1.0", "CRA-2.0"): {"absorbed": True}}
    assert graph.status_of("CRA-1.0") == "superseded"
    assert graph.status_of("CRA-2.0") == "active"
    assert graph.marker_stage("CRA-2.0") == "linked"


def test_link_and_supersede_twice_leaves_the_same_state() -> None:
    graph = LedgerNativeGraph()
    graph.seed_instrument("CRA-1.0")
    graph.seed_instrument("CRA-2.0")

    link_and_supersede(graph, "CRA-1.0", "CRA-2.0")
    link_and_supersede(graph, "CRA-1.0", "CRA-2.0")

    assert len(graph.edges) == 1
    assert graph.marker_stage("CRA-2.0") == "linked"


# --- progress marker ----------------------------------------------------


def test_mark_stage_complete_upserts_the_marker_with_the_stage() -> None:
    graph = FakeGraph()

    mark_stage_complete(graph, "CRA-2.0", "extraction")

    assert graph.calls[0].query == _MARK_QUERY
    assert graph.calls[0].params == {"new_id": "CRA-2.0", "stage": "extraction"}


def test_mark_stage_complete_overwrites_the_previous_stage() -> None:
    graph = LedgerNativeGraph()

    mark_stage_complete(graph, "CRA-2.0", "ingestion")
    mark_stage_complete(graph, "CRA-2.0", "extraction")

    assert graph.marker_stage("CRA-2.0") == "extraction"


def test_clear_marker_is_a_plain_match_delete_and_idempotent() -> None:
    graph = LedgerNativeGraph()
    graph.seed_marker("CRA-2.0", "linked")

    clear_marker(graph, "CRA-2.0")
    clear_marker(graph, "CRA-2.0")

    assert [call.query for call in graph.calls] == [_CLEAR_QUERY, _CLEAR_QUERY]
    assert graph.marker_stage("CRA-2.0") is None


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


# --- issue #201 (D2): the `policy_system` side of the succession ---

_SINGLE_TENANT_QUERY = """\
MATCH (prior:RegulatoryInstrument {id: $prior_id}),
      (new:RegulatoryInstrument {id: $new_id})
MERGE (prior)-[:SUPERSEDED_BY]->(new)
SET prior.status = 'superseded'
RETURN prior.id AS prior_id"""


def test_supersede_in_single_tenant_issues_the_exact_statement() -> None:
    graph = FakeGraph([FakeQueryResult([["CRA-1.0"]])])

    supersede_in_single_tenant(graph, "CRA-1.0", "CRA-2.0")

    assert [(c.query, c.params) for c in graph.calls] == [
        (_SINGLE_TENANT_QUERY, {"prior_id": "CRA-1.0", "new_id": "CRA-2.0"})
    ]


def test_supersede_in_single_tenant_writes_edge_and_status_and_is_idempotent() -> None:
    graph = LedgerSingleTenantGraph()
    graph.seed_instrument("CRA-1.0")
    graph.seed_instrument("CRA-2.0")

    supersede_in_single_tenant(graph, "CRA-1.0", "CRA-2.0")
    supersede_in_single_tenant(graph, "CRA-1.0", "CRA-2.0")

    assert graph.edges == {("CRA-1.0", "CRA-2.0")}
    assert graph.status_of("CRA-1.0") == "superseded"
    assert graph.status_of("CRA-2.0") == "active"


@pytest.mark.parametrize("missing", ["CRA-1.0", "CRA-2.0"])
def test_supersede_in_single_tenant_with_a_missing_node_is_an_inconsistent_graph(
    missing: str,
) -> None:
    graph = LedgerSingleTenantGraph()
    for node_id in ("CRA-1.0", "CRA-2.0"):
        if node_id != missing:
            graph.seed_instrument(node_id)

    with pytest.raises(ChangeMonitorStateError, match=missing):
        supersede_in_single_tenant(graph, "CRA-1.0", "CRA-2.0")

    assert graph.edges == set()


def test_supersede_in_single_tenant_wraps_redis_error() -> None:
    with pytest.raises(SuccessionPersistenceError):
        supersede_in_single_tenant(RaisingGraph(), "CRA-1.0", "CRA-2.0")
