"""Writer A2 for `merge-obligations` (issue #190, slice 11 sub-step b): call shape.

The real Cypher semantics are proven only by the `falkordb_live` twin; here the statement
contract is pinned (one call, guard params, error mapping, AC-BI-015 guard in the text,
AC-BI-018).
"""

from __future__ import annotations

import pytest
import redis.exceptions

from graph_cleanup._fakes import OBL_ABSORBED, OBL_SURVIVOR, ScriptedObligationGraph
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
)
from ps_service.graph_cleanup.graph_writer import MERGE_OBLIGATIONS_QUERY, merge_obligations
from ps_service.graph_cleanup.models import ObligationExpectedCounts

_EXPECTED = ObligationExpectedCounts(satisfied=2, requires=1)


def test_issues_exactly_one_statement_with_both_ids_and_the_expected_counts() -> None:
    graph = ScriptedObligationGraph()

    merge_obligations(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED, expected=_EXPECTED)

    assert len(graph.queries) == 1
    query, params = graph.queries[0]
    assert query == MERGE_OBLIGATIONS_QUERY
    assert params == {
        "survivor_id": OBL_SURVIVOR,
        "absorbed_id": OBL_ABSORBED,
        "expected_satisfied": 2,
        "expected_requires": 1,
    }


def test_the_statement_requires_a_shared_role_and_distinct_nodes() -> None:
    assert "(r:Role)-[:HAS]->(s:Obligation {id: $survivor_id})" in MERGE_OBLIGATIONS_QUERY
    assert "(r)-[:HAS]->(a:Obligation {id: $absorbed_id})" in MERGE_OBLIGATIONS_QUERY
    assert "s <> a" in MERGE_OBLIGATIONS_QUERY


def test_the_statement_writes_the_marker_and_deletes_the_absorbed_node_with_its_edges() -> None:
    assert "MERGE (m:MergedObligation {id: $absorbed_id})" in MERGE_OBLIGATIONS_QUERY
    assert "SET m.merged_into = $survivor_id" in MERGE_OBLIGATIONS_QUERY
    assert "DETACH DELETE a" in MERGE_OBLIGATIONS_QUERY
    assert MERGE_OBLIGATIONS_QUERY.index("MERGE (x)-[:SATISFIED_BY]->(s)") < (
        MERGE_OBLIGATIONS_QUERY.index("DETACH DELETE a")
    )


def test_a_guard_miss_is_a_stale_state_error() -> None:
    graph = ScriptedObligationGraph(write_rows=[])

    with pytest.raises(GraphCleanupStaleStateError):
        merge_obligations(
            graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED, expected=_EXPECTED
        )


def test_a_driver_failure_is_a_generic_error_without_internal_detail() -> None:
    graph = ScriptedObligationGraph(
        write_error=redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")
    )

    with pytest.raises(GraphCleanupPersistenceError) as excinfo:
        merge_obligations(
            graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED, expected=_EXPECTED
        )

    message = str(excinfo.value)
    assert "10.1.2.3" not in message
    assert "6379" not in message
    assert "refused" not in message
