"""Writer A4 for `release-capability-governance` (issue #190, slice 14 b): call shape + contract.

Real Cypher semantics are proven only by the `falkordb_live` twin; here the statement contract
is pinned (one guarded statement, draft-only guard before any write, AC-BI-018 error mapping).
"""

from __future__ import annotations

import re

import pytest
import redis.exceptions

from graph_cleanup._fakes import SURVIVOR, ScriptedReleaseGraph
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
)
from ps_service.graph_cleanup.graph_writer import RELEASE_GOVERNANCE_QUERY, release_governance

_WRITE_KEYWORD = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|FOREACH)\b")


def test_issues_exactly_one_statement_with_the_capability_and_policy_ids() -> None:
    graph = ScriptedReleaseGraph()

    release_governance(graph, capability_id=SURVIVOR, policy_id="pol_1")

    assert graph.queries == [
        (RELEASE_GOVERNANCE_QUERY, {"capability_id": SURVIVOR, "policy_id": "pol_1"})
    ]


def test_the_statement_deletes_only_the_edge_and_only_from_a_draft_policy() -> None:
    text = RELEASE_GOVERNANCE_QUERY
    guard = text.index("DELETE")

    assert not _WRITE_KEYWORD.search(text[:guard])
    assert "status: 'draft'" in text
    assert "DETACH" not in text
    assert text.count("DELETE") == 1
    assert text.count("RETURN") == 1
    assert ";" not in text
    assert set(re.findall(r"\$(\w+)", text)) == {"capability_id", "policy_id"}
    assert "coalesce(c.status,'active') = 'active'" in text


def test_a_guard_miss_is_a_stale_state_error() -> None:
    graph = ScriptedReleaseGraph(release_rows=[])

    with pytest.raises(GraphCleanupStaleStateError):
        release_governance(graph, capability_id=SURVIVOR, policy_id="pol_1")


def test_a_driver_failure_is_a_generic_error_without_internal_detail() -> None:
    graph = ScriptedReleaseGraph(
        release_error=redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")
    )

    with pytest.raises(GraphCleanupPersistenceError) as excinfo:
        release_governance(graph, capability_id=SURVIVOR, policy_id="pol_1")

    assert "10.1.2.3" not in str(excinfo.value)
    assert "6379" not in str(excinfo.value)
