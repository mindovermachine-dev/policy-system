"""Reader for `release-capability-governance` (issue #190, slice 14 b): pure reads."""

from __future__ import annotations

import re

import pytest
import redis.exceptions

from graph_cleanup._fakes import SURVIVOR, ScriptedReleaseGraph
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.graph_reader import (
    read_governed_by_edge_present,
    read_release_state,
)

_WRITE_KEYWORD = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DETACH)\b")


def test_state_carries_the_capability_its_policy_and_the_policys_governed_set() -> None:
    graph = ScriptedReleaseGraph()

    state = read_release_state(graph, capability_id=SURVIVOR)

    assert state.capability is not None
    assert state.capability.id == SURVIVOR
    assert state.capability.status == "active"
    assert [(p.id, p.title, p.status) for p in state.policies] == [
        ("pol_1", "Incident Policy", "draft")
    ]
    assert set(state.governed_set) == {SURVIVOR, "cap_other"}
    assert all("embedding" not in query for query, _ in graph.queries)


def test_a_missing_capability_is_reported_as_none_and_an_ungoverned_one_has_no_policy() -> None:
    missing = read_release_state(ScriptedReleaseGraph(nodes=[]), capability_id=SURVIVOR)
    ungoverned = read_release_state(
        ScriptedReleaseGraph(governors=[["cap_other", "pol_9", "X", "draft"]]),
        capability_id=SURVIVOR,
    )

    assert missing.capability is None
    assert ungoverned.policies == ()


def test_the_edge_presence_read_reports_the_count() -> None:
    assert read_governed_by_edge_present(
        ScriptedReleaseGraph(edge_count=1), capability_id=SURVIVOR, policy_id="pol_1"
    )
    assert not read_governed_by_edge_present(
        ScriptedReleaseGraph(edge_count=0), capability_id=SURVIVOR, policy_id="pol_1"
    )


def test_every_read_is_write_free_and_parameterised() -> None:
    graph = ScriptedReleaseGraph()

    read_release_state(graph, capability_id=SURVIVOR)
    read_governed_by_edge_present(graph, capability_id=SURVIVOR, policy_id="pol_1")

    for query, params in graph.queries:
        assert not _WRITE_KEYWORD.search(query)
        assert params is not None
        assert SURVIVOR not in query


def test_a_driver_failure_is_a_generic_persistence_error() -> None:
    graph = ScriptedReleaseGraph(read_error=redis.exceptions.ConnectionError("host=10.0.0.1"))

    with pytest.raises(GraphCleanupPersistenceError) as excinfo:
        read_release_state(graph, capability_id=SURVIVOR)

    assert "10.0.0.1" not in str(excinfo.value)
