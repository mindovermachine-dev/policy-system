"""Reader for `merge-capabilities` (issue #190, slice 10 sub-step b): pure reads."""

from __future__ import annotations

import re

import pytest
import redis.exceptions

from graph_cleanup._fakes import ABSORBED, SURVIVOR, ScriptedMergeGraph
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_capability_tombstone,
)

_WRITE_KEYWORD = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DETACH)\b")


def test_state_carries_nodes_edges_and_properties_without_embedding() -> None:
    graph = ScriptedMergeGraph()

    state = read_capability_merge_state(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert state.survivor is not None
    assert state.survivor.properties == {"description": "desc", "confidence": 0.9}
    assert state.absorbed is not None
    assert state.absorbed.properties == {"type": "technical", "confidence": 0.8}
    assert {(e.rel_type, e.source_id, e.target_id) for e in state.edges} == {
        ("REQUIRES", "obl_1", ABSORBED),
        ("REQUIRES", "obl_2", ABSORBED),
        ("REQUIRES", "obl_2", SURVIVOR),
        ("COVERS", "pa_1", ABSORBED),
    }
    assert state.survivor_policies == ()
    assert all("embedding" not in query for query, _ in graph.queries)


def test_a_missing_node_is_reported_as_none() -> None:
    graph = ScriptedMergeGraph(nodes=[[SURVIVOR, "Survivor", "active", None, None, None]])

    state = read_capability_merge_state(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert state.absorbed is None


def test_governing_policies_are_attributed_to_their_capability() -> None:
    graph = ScriptedMergeGraph(governors=[[ABSORBED, "pol_1", "Policy", "draft"]])

    state = read_capability_merge_state(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert state.survivor_policies == ()
    assert [p.id for p in state.absorbed_policies] == ["pol_1"]


def test_every_read_query_is_write_free_and_parameterised() -> None:
    graph = ScriptedMergeGraph()

    read_capability_merge_state(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    read_capability_tombstone(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert graph.queries
    for query, params in graph.queries:
        assert not _WRITE_KEYWORD.search(query.upper())
        assert params is not None
        assert SURVIVOR not in query
        assert ABSORBED not in query


def test_a_driver_failure_is_a_generic_persistence_error() -> None:
    graph = ScriptedMergeGraph(read_error=redis.exceptions.ConnectionError("host=10.1.2.3 refused"))

    with pytest.raises(GraphCleanupPersistenceError) as excinfo:
        read_capability_merge_state(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert "10.1.2.3" not in str(excinfo.value)


@pytest.mark.parametrize(("count", "expected"), [(1, True), (0, False)])
def test_tombstone_read(count: int, *, expected: bool) -> None:
    graph = ScriptedMergeGraph(tombstone_count=count)

    assert read_capability_tombstone(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED) is expected


def test_governed_sets_list_every_capability_a_governing_policy_governs() -> None:
    graph = ScriptedMergeGraph(governors=[[ABSORBED, "pol_1", "Policy", "approved"]])

    state = read_capability_merge_state(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert state.governed_sets == {"pol_1": (ABSORBED, "cap_other")}
    set_queries = [q for q, _ in graph.queries if "p.id IN $policy_ids" in q]
    assert len(set_queries) == 1
    assert not _WRITE_KEYWORD.search(set_queries[0].upper())


def test_no_governed_set_query_is_issued_when_neither_capability_is_governed() -> None:
    graph = ScriptedMergeGraph()

    state = read_capability_merge_state(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)

    assert state.governed_sets == {}
    assert all("p.id IN $policy_ids" not in q for q, _ in graph.queries)
