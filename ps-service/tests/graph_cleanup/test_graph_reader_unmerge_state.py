"""Reader for capability `unmerge` (issue #190, slice 15 sub-step b): live state around a tombstone.

Pure reads with literal parameterised queries; a driver failure is a generic error.
"""

from __future__ import annotations

import pytest
import redis.exceptions

from graph_cleanup._fakes import ABSORBED, SURVIVOR, ScriptedUnmergeGraph
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.graph_reader import (
    read_capability_unmerge_state,
    read_capability_unmerged,
)
from ps_service.graph_cleanup.models import CapabilityUnmergeInputs

_WRITE = ("CREATE", "MERGE ", "DELETE", "SET ", "REMOVE")


def _inputs(**overrides: object) -> CapabilityUnmergeInputs:
    base: dict[str, object] = {
        "merge_approval_id": "merge-approval-1",
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "policy_case": 1,
        "restore_requires": ("obl_1", "obl_2"),
        "restore_covers": ("pa_1",),
        "restore_mitigated": ("rp_1",),
        "remove_requires": ("obl_1",),
        "remove_covers": ("pa_1",),
        "remove_mitigated": ("rp_1",),
        "restore_policy_id": None,
        "remove_policy_edge": False,
        "snapshot_survivor_edges": (),
    }
    return CapabilityUnmergeInputs.model_validate({**base, **overrides})


def test_reads_both_nodes_the_redirects_the_survivor_edges_and_the_endpoints() -> None:
    graph = ScriptedUnmergeGraph()

    state = read_capability_unmerge_state(graph, inputs=_inputs())

    assert state.absorbed is not None
    assert (state.absorbed.id, state.absorbed.status) == (ABSORBED, "merged")
    assert state.survivor is not None
    assert state.survivor.status == "active"
    assert state.redirects == {ABSORBED: (SURVIVOR,)}
    assert {(e.rel_type, e.source_id) for e in state.survivor_edges} == {
        ("REQUIRES", "obl_1"),
        ("REQUIRES", "obl_2"),
        ("COVERS", "pa_1"),
        ("MITIGATED_BY", "rp_1"),
    }
    assert state.existing_endpoints == {
        "REQUIRES": ("obl_1", "obl_2"),
        "COVERS": ("pa_1",),
        "MITIGATED_BY": ("rp_1",),
    }
    assert state.survivor_policy_ids == ()
    assert state.policy_exists is True


def test_a_missing_endpoint_is_absent_from_the_existing_set() -> None:
    graph = ScriptedUnmergeGraph(
        existing={"Obligation": ["obl_2"], "PracticeArea": [], "RiskPath": []}
    )

    state = read_capability_unmerge_state(graph, inputs=_inputs())

    assert state.existing_endpoints["REQUIRES"] == ("obl_2",)
    assert state.existing_endpoints["COVERS"] == ()


def test_the_governing_policy_is_read_only_when_the_snapshot_needs_it() -> None:
    graph = ScriptedUnmergeGraph(governors=[[SURVIVOR, "pol_1", "T", "approved"]])

    state = read_capability_unmerge_state(
        graph, inputs=_inputs(restore_policy_id="pol_1", remove_policy_edge=True)
    )

    assert state.survivor_policy_ids == ("pol_1",)
    assert state.policy_exists is True
    gone = ScriptedUnmergeGraph(policy_rows=[])
    assert (
        read_capability_unmerge_state(
            gone, inputs=_inputs(restore_policy_id="pol_1", remove_policy_edge=True)
        ).policy_exists
        is False
    )


def test_missing_nodes_are_reported_as_none() -> None:
    graph = ScriptedUnmergeGraph(nodes=[], redirects=[])

    state = read_capability_unmerge_state(graph, inputs=_inputs())

    assert state.absorbed is None
    assert state.survivor is None


def test_every_read_is_a_literal_parameterised_query_with_no_write_keyword() -> None:
    graph = ScriptedUnmergeGraph()

    read_capability_unmerge_state(graph, inputs=_inputs())

    assert graph.queries
    for text, params in graph.queries:
        assert params is not None
        assert not any(word in text for word in _WRITE)
        assert SURVIVOR not in text
        assert ABSORBED not in text


def test_a_driver_failure_is_a_generic_persistence_error() -> None:
    graph = ScriptedUnmergeGraph(
        read_error=redis.exceptions.ConnectionError("host=10.1.2.3 port=6379")
    )

    with pytest.raises(GraphCleanupPersistenceError) as caught:
        read_capability_unmerge_state(graph, inputs=_inputs())

    assert "10.1.2.3" not in str(caught.value)


def test_unmerged_is_true_for_an_active_node_without_a_redirect_only() -> None:
    assert read_capability_unmerged(
        ScriptedUnmergeGraph(unmerged_row=[1, 0]), capability_id=ABSORBED
    )
    assert not read_capability_unmerged(
        ScriptedUnmergeGraph(unmerged_row=[1, 1]), capability_id=ABSORBED
    )
    assert not read_capability_unmerged(
        ScriptedUnmergeGraph(unmerged_row=[0, 0]), capability_id=ABSORBED
    )
