"""Reader for obligation `unmerge` (issue #190, slice 16 sub-step b): live state around a marker.

Pure reads with literal parameterised queries; a driver failure is a generic error.
"""

from __future__ import annotations

import pytest
import redis.exceptions

from graph_cleanup._fakes import (
    OBL_ABSORBED,
    OBL_SURVIVOR,
    ROLE_ID,
    ScriptedObligationUnmergeGraph,
)
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.graph_reader import read_obligation_unmerge_state
from ps_service.graph_cleanup.models import ObligationUnmergeInputs

_WRITE = ("CREATE", "MERGE ", "DELETE", "SET ", "REMOVE")


def _inputs() -> ObligationUnmergeInputs:
    return ObligationUnmergeInputs(
        merge_approval_id="merge-approval-1",
        survivor_id=OBL_SURVIVOR,
        absorbed_id=OBL_ABSORBED,
        role_id=ROLE_ID,
        properties={"text": "Report  incidents.", "confidence": 0.8},
        satisfied_by_ids=("req_1", "req_2"),
        requires_ids=("cap_1",),
        snapshot_survivor_edges=(),
        survivor_edges_before_merge=(),
    )


def test_reads_the_marker_the_survivor_the_role_the_endpoints_and_the_survivors_edges() -> None:
    state = read_obligation_unmerge_state(ScriptedObligationUnmergeGraph(), inputs=_inputs())

    assert state.absorbed_exists is False
    assert state.survivor_exists is True
    assert state.survivor_text == "Report incidents"
    assert state.marker_targets == {OBL_ABSORBED: (OBL_SURVIVOR,)}
    assert state.role_exists is True
    assert state.existing_requirements == ("req_1", "req_2")
    assert state.capability_statuses == {"cap_1": "active"}
    assert {(e.rel_type, e.source_id, e.target_id) for e in state.survivor_edges} == {
        ("SATISFIED_BY", "req_1", OBL_SURVIVOR),
        ("SATISFIED_BY", "req_2", OBL_SURVIVOR),
        ("REQUIRES", OBL_SURVIVOR, "cap_1"),
    }


def test_an_absorbed_obligation_that_exists_again_is_reported() -> None:
    graph = ScriptedObligationUnmergeGraph(
        nodes=[[OBL_SURVIVOR, "Report incidents", 0.9], [OBL_ABSORBED, "Again", 0.5]]
    )

    state = read_obligation_unmerge_state(graph, inputs=_inputs())

    assert state.absorbed_exists is True


def test_missing_things_are_reported_as_absent() -> None:
    graph = ScriptedObligationUnmergeGraph(
        nodes=[], markers=[], roles=[], requirements=[["req_2"]], capabilities=[]
    )

    state = read_obligation_unmerge_state(graph, inputs=_inputs())

    assert state.survivor_exists is False
    assert state.marker_targets == {}
    assert state.role_exists is False
    assert state.existing_requirements == ("req_2",)
    assert state.capability_statuses == {}


def test_every_read_is_a_literal_parameterised_query_with_no_write_keyword() -> None:
    graph = ScriptedObligationUnmergeGraph()

    read_obligation_unmerge_state(graph, inputs=_inputs())

    assert graph.queries
    for text, params in graph.queries:
        assert params is not None
        assert not any(word in text for word in _WRITE)
        assert OBL_SURVIVOR not in text
        assert OBL_ABSORBED not in text


def test_a_driver_failure_is_a_generic_persistence_error() -> None:
    graph = ScriptedObligationUnmergeGraph(
        read_error=redis.exceptions.ConnectionError("host=10.1.2.3 port=6379")
    )

    with pytest.raises(GraphCleanupPersistenceError) as caught:
        read_obligation_unmerge_state(graph, inputs=_inputs())

    assert "10.1.2.3" not in str(caught.value)
