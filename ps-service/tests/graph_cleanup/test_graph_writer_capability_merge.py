"""Writer A1 for `merge-capabilities` (issue #190, slice 10 sub-step b): call shape.

The real Cypher semantics are proven only by the `falkordb_live` twin; here the
statement contract is pinned (one call, guard params, error mapping, AC-BI-018).
"""

from __future__ import annotations

import pytest
import redis.exceptions

from graph_cleanup._fakes import ABSORBED, SURVIVOR, ScriptedMergeGraph
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
)
from ps_service.graph_cleanup.graph_writer import MERGE_CAPABILITIES_QUERY, merge_capabilities
from ps_service.graph_cleanup.models import ExpectedCounts

_EXPECTED = ExpectedCounts(requires=2, covers=1, mitigated=0)


def test_issues_exactly_one_statement_with_both_ids_and_the_expected_counts() -> None:
    graph = ScriptedMergeGraph()

    merge_capabilities(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED, expected=_EXPECTED)

    assert len(graph.queries) == 1
    query, params = graph.queries[0]
    assert query == MERGE_CAPABILITIES_QUERY
    assert params == {
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "expected_requires": 2,
        "expected_covers": 1,
        "expected_mitigated": 0,
        "expected_absorbed_governed": 0,
        "expected_survivor_governed": 0,
        "expected_absorbed_policy_id": None,
        "expected_absorbed_policy_status": None,
        "expected_survivor_policy_id": None,
        "expected_survivor_policy_status": None,
    }


def test_a_guard_miss_is_a_stale_state_error() -> None:
    graph = ScriptedMergeGraph(write_rows=[])

    with pytest.raises(GraphCleanupStaleStateError):
        merge_capabilities(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED, expected=_EXPECTED)


def test_a_driver_failure_is_a_generic_error_without_internal_detail() -> None:
    graph = ScriptedMergeGraph(
        write_error=redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")
    )

    with pytest.raises(GraphCleanupPersistenceError) as excinfo:
        merge_capabilities(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED, expected=_EXPECTED)

    message = str(excinfo.value)
    assert "10.1.2.3" not in message
    assert "6379" not in message
    assert "refused" not in message
    assert isinstance(excinfo.value.__cause__, GraphCleanupPersistenceError)
