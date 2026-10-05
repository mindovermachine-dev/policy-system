"""Reader for `merge-obligations` (issue #190, slice 11 sub-step b): pure reads."""

from __future__ import annotations

import re

import pytest
import redis.exceptions

from graph_cleanup._fakes import (
    OBL_ABSORBED,
    OBL_SURVIVOR,
    ROLE_ID,
    ScriptedObligationGraph,
)
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.graph_reader import (
    read_obligation_marker,
    read_obligation_merge_state,
    read_obligation_present,
)

_WRITE_KEYWORD = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DETACH)\b")


def test_state_carries_nodes_roles_edges_and_source_refs() -> None:
    graph = ScriptedObligationGraph()

    state = read_obligation_merge_state(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)

    assert state.survivor is not None
    assert state.survivor.text == "Report incidents"
    assert state.survivor.properties == {"confidence": 0.9}
    assert state.absorbed is not None
    assert state.absorbed.properties == {"confidence": 0.8}
    assert [r.id for r in state.survivor_roles] == [ROLE_ID]
    assert [r.name for r in state.absorbed_roles] == ["Manufacturer"]
    assert {(e.rel_type, e.source_id, e.target_id) for e in state.edges} == {
        ("SATISFIED_BY", "req_1", OBL_ABSORBED),
        ("SATISFIED_BY", "req_2", OBL_ABSORBED),
        ("SATISFIED_BY", "req_2", OBL_SURVIVOR),
        ("REQUIRES", OBL_ABSORBED, "cap_1"),
        ("REQUIRES", OBL_SURVIVOR, "cap_1"),
    }
    assert [(r.requirement_id, r.source_ref) for r in state.absorbed_requirement_refs] == [
        ("req_1", "Art. 6(1)"),
        ("req_2", "Art. 6(2)"),
    ]


def test_a_missing_node_is_reported_as_none() -> None:
    graph = ScriptedObligationGraph(nodes=[[OBL_SURVIVOR, "t", 0.9]])

    state = read_obligation_merge_state(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)

    assert state.absorbed is None


def test_every_read_query_is_write_free_and_parameterised() -> None:
    graph = ScriptedObligationGraph()

    read_obligation_merge_state(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)
    read_obligation_marker(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)
    read_obligation_present(graph, obligation_id=OBL_ABSORBED)

    assert graph.queries
    for query, params in graph.queries:
        assert not _WRITE_KEYWORD.search(query.upper())
        assert params is not None
        assert OBL_SURVIVOR not in query
        assert OBL_ABSORBED not in query


def test_a_driver_failure_is_a_generic_persistence_error() -> None:
    graph = ScriptedObligationGraph(
        read_error=redis.exceptions.ConnectionError("host=10.1.2.3 refused")
    )

    with pytest.raises(GraphCleanupPersistenceError) as excinfo:
        read_obligation_merge_state(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)

    assert "10.1.2.3" not in str(excinfo.value)


@pytest.mark.parametrize(("count", "expected"), [(1, True), (0, False)])
def test_marker_and_presence_reads(count: int, *, expected: bool) -> None:
    graph = ScriptedObligationGraph(marker_count=count, absorbed_count=count)

    assert (
        read_obligation_marker(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)
        is expected
    )
    assert read_obligation_present(graph, obligation_id=OBL_ABSORBED) is expected
