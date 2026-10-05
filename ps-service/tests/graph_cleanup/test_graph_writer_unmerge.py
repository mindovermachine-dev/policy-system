"""Writer A5 for capability `unmerge` (issue #190, slice 15 sub-step b): call shape + contract.

Real Cypher semantics are proven only by the `falkordb_live` twin; here the statement contract
is pinned (one guarded statement, guard before any write, AC-BI-018 error mapping).
"""

from __future__ import annotations

import re

import pytest
import redis.exceptions

from graph_cleanup._fakes import ABSORBED, SURVIVOR, ScriptedUnmergeGraph
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
)
from ps_service.graph_cleanup.graph_writer import UNMERGE_CAPABILITY_QUERY, unmerge_capability
from ps_service.graph_cleanup.models import CapabilityUnmergeWrite

_WRITE = CapabilityUnmergeWrite(
    restore_requires_ids=("obl_1", "obl_2"),
    remove_requires_ids=("obl_1",),
    restore_covers_ids=("pa_1",),
    remove_covers_ids=("pa_1",),
    restore_mitigated_ids=(),
    remove_mitigated_ids=(),
    restore_policy_id="pol_1",
    remove_policy_edge=True,
)


def test_issues_exactly_one_statement_with_every_parameter_the_text_uses() -> None:
    graph = ScriptedUnmergeGraph()

    unmerge_capability(graph, absorbed_id=ABSORBED, survivor_id=SURVIVOR, write=_WRITE)

    [(text, params)] = graph.queries
    assert text == UNMERGE_CAPABILITY_QUERY
    assert params is not None
    assert set(re.findall(r"\$(\w+)", text)) == set(params)
    assert params["absorbed_id"] == ABSORBED
    assert params["survivor_id"] == SURVIVOR
    assert params["restore_requires_ids"] == ["obl_1", "obl_2"]
    assert params["remove_requires_ids"] == ["obl_1"]
    assert params["restore_policy_id"] == "pol_1"
    assert params["remove_policy_edge"] is True


def test_no_policy_to_restore_is_passed_as_null() -> None:
    graph = ScriptedUnmergeGraph()
    write = _WRITE.model_copy(update={"restore_policy_id": None, "remove_policy_edge": False})

    unmerge_capability(graph, absorbed_id=ABSORBED, survivor_id=SURVIVOR, write=write)

    params = graph.queries[0][1]
    assert params is not None
    assert params["restore_policy_id"] is None
    assert params["remove_policy_edge"] is False


def test_the_statement_pins_the_tombstone_the_survivor_and_every_count_before_writing() -> None:
    text = UNMERGE_CAPABILITY_QUERY

    assert "status: 'merged'" in text
    assert "-[m:MERGED_INTO]->(s:Capability {id: $survivor_id})" in text
    assert "coalesce(s.status,'active') = 'active'" in text
    for name in ("requires", "covers", "mitigated"):
        assert f"size($restore_{name}_ids)" in text
        assert f"size($remove_{name}_ids)" in text
    assert "SET a.status = 'active'" in text
    assert "DELETE m" in text
    assert "DETACH" not in text
    assert text.count("RETURN") == 1


def test_a_guard_miss_is_a_stale_state_error() -> None:
    graph = ScriptedUnmergeGraph(write_rows=[])

    with pytest.raises(GraphCleanupStaleStateError):
        unmerge_capability(graph, absorbed_id=ABSORBED, survivor_id=SURVIVOR, write=_WRITE)


def test_a_driver_failure_is_a_generic_error_without_internal_detail() -> None:
    graph = ScriptedUnmergeGraph(
        write_error=redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")
    )

    with pytest.raises(GraphCleanupPersistenceError) as excinfo:
        unmerge_capability(graph, absorbed_id=ABSORBED, survivor_id=SURVIVOR, write=_WRITE)

    assert "10.1.2.3" not in str(excinfo.value)
    assert "6379" not in str(excinfo.value)
