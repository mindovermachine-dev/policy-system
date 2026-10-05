"""Writer A6 for obligation `unmerge` (issue #190, slice 16 sub-step b): call shape + contract.

Real Cypher semantics are proven only by the `falkordb_live` twin; here the statement contract
is pinned (one guarded statement, guard before any write, AC-BI-018 error mapping).
"""

from __future__ import annotations

import re

import pytest
import redis.exceptions

from graph_cleanup._fakes import (
    OBL_ABSORBED,
    OBL_SURVIVOR,
    ROLE_ID,
    ScriptedObligationUnmergeGraph,
)
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
)
from ps_service.graph_cleanup.graph_writer import UNMERGE_OBLIGATION_QUERY, unmerge_obligation
from ps_service.graph_cleanup.models import ObligationUnmergeWrite

_WRITE = ObligationUnmergeWrite(
    role_id=ROLE_ID,
    satisfied_by_ids=("req_1", "req_2"),
    requires_ids=("cap_1",),
    properties={"text": "Report  incidents.", "confidence": 0.8},
)


def test_issues_exactly_one_statement_with_every_parameter_the_text_uses() -> None:
    graph = ScriptedObligationUnmergeGraph()

    unmerge_obligation(graph, absorbed_id=OBL_ABSORBED, survivor_id=OBL_SURVIVOR, write=_WRITE)

    [(text, params)] = graph.queries
    assert text == UNMERGE_OBLIGATION_QUERY
    assert params is not None
    assert set(re.findall(r"\$(\w+)", text)) == set(params)
    assert params["absorbed_id"] == OBL_ABSORBED
    assert params["survivor_id"] == OBL_SURVIVOR
    assert params["role_id"] == ROLE_ID
    assert params["satisfied_by_ids"] == ["req_1", "req_2"]
    assert params["requires_ids"] == ["cap_1"]
    assert params["properties"] == {"text": "Report  incidents.", "confidence": 0.8}


def test_the_statement_recreates_under_the_original_id_and_removes_only_the_marker() -> None:
    text = UNMERGE_OBLIGATION_QUERY

    assert (
        "MATCH (m:MergedObligation {id: $absorbed_id}) WHERE m.merged_into = $survivor_id" in text
    )
    assert "CREATE (o:Obligation {id: $absorbed_id})" in text
    assert "SET o += $properties" in text
    assert "MERGE (r)-[:HAS]->(o)" in text
    assert "DELETE m" in text
    assert "DETACH" not in text
    assert "size(existing) = 0" in text
    assert "<> 'merged'" in text
    assert text.count("RETURN") == 1
    assert ";" not in text


def test_a_guard_miss_is_a_stale_state_error() -> None:
    graph = ScriptedObligationUnmergeGraph(write_rows=[])

    with pytest.raises(GraphCleanupStaleStateError):
        unmerge_obligation(graph, absorbed_id=OBL_ABSORBED, survivor_id=OBL_SURVIVOR, write=_WRITE)


def test_a_driver_failure_is_a_generic_error_without_internal_detail() -> None:
    graph = ScriptedObligationUnmergeGraph(
        write_error=redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")
    )

    with pytest.raises(GraphCleanupPersistenceError) as excinfo:
        unmerge_obligation(graph, absorbed_id=OBL_ABSORBED, survivor_id=OBL_SURVIVOR, write=_WRITE)

    assert "10.1.2.3" not in str(excinfo.value)
    assert "6379" not in str(excinfo.value)
