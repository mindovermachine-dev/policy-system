"""Near-miss merge and `merged` Capability tombstones (issue #190).

Two guarantees. First, a `PendingReview` naming a tombstone on either side is
stale: `resolve_review` raises `StalePendingReviewError` before the merge write
runs, so the tombstone and its `MERGED_INTO` redirect survive. Second, the merge
write re-points every inbound `MERGED_INTO` edge from the deleted loser onto the
winner (statement-shape tests here; the real Cypher is proven by the
`falkordb_live` test at the bottom).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.company_merge.errors import StalePendingReviewError
from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.company_merge.pending_review import resolve_review
from ps_service.config import load_config

if TYPE_CHECKING:
    from collections.abc import Iterable

    from company_merge._fakes import MakeEmitter


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _TombstoneAwareGraph:
    """Evaluates the existence check's status predicate the way FalkorDB would.

    An endpoint whose id is in `merged_ids` counts as existing only when the
    existence query text carries no `<> 'merged'` exclusion for it -- so the
    test turns red on the query text, not on a scripted boolean.
    """

    def __init__(self, *, incoming_id: str, existing_id: str, merged_ids: Iterable[str]) -> None:
        self._incoming_id = incoming_id
        self._existing_id = existing_id
        self._merged_ids = frozenset(merged_ids)
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        if "RETURN r.kind" in q:
            return _FakeQueryResult([["Capability"]])
        if "incoming_exists" in q:
            incoming_excluded = "coalesce(a.status, 'active') <> 'merged'" in q
            existing_excluded = "coalesce(b.status, 'active') <> 'merged'" in q
            incoming_exists = not (incoming_excluded and self._incoming_id in self._merged_ids)
            existing_exists = not (existing_excluded and self._existing_id in self._merged_ids)
            return _FakeQueryResult(
                [[self._incoming_id, self._existing_id, incoming_exists, existing_exists]]
            )
        return _FakeQueryResult([[self._existing_id, self._incoming_id]])

    def merge_write_calls(self) -> list[_RecordedCall]:
        return [c for c in self.calls if "DETACH DELETE" in c.query]


@pytest.mark.parametrize(
    ("incoming_id", "existing_id", "merged_ids"),
    [
        pytest.param("cap_tomb", "cap_live", {"cap_tomb"}, id="incoming-is-tombstone"),
        pytest.param("cap_live", "cap_tomb", {"cap_tomb"}, id="existing-is-tombstone"),
    ],
)
def test_review_naming_a_tombstone_is_stale_and_issues_no_merge_write(
    incoming_id: str, existing_id: str, merged_ids: set[str]
) -> None:
    graph = _TombstoneAwareGraph(
        incoming_id=incoming_id, existing_id=existing_id, merged_ids=merged_ids
    )

    with pytest.raises(StalePendingReviewError):
        resolve_review(graph, "review_aaa", "merge")

    assert graph.merge_write_calls() == []


def test_review_between_two_active_capabilities_still_merges(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    graph = _TombstoneAwareGraph(incoming_id="cap_a", existing_id="cap_b", merged_ids=set())

    outcome = resolve_review(graph, "review_aaa", "merge", emitter=emitter)

    assert outcome is not None
    assert len(graph.merge_write_calls()) == 1


def test_existence_check_carries_the_predicate_on_both_sides(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    graph = _TombstoneAwareGraph(incoming_id="pol_a", existing_id="pol_b", merged_ids=set())

    resolve_review(graph, "review_aaa", "merge", emitter=emitter)

    existence = next(c.query for c in graph.calls if "incoming_exists" in c.query)
    assert existence.count("<> 'merged'") == 2


def test_merge_statement_repoints_inbound_merged_into_before_deleting_the_loser(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    graph = _TombstoneAwareGraph(incoming_id="cap_a", existing_id="cap_b", merged_ids=set())

    resolve_review(graph, "review_aaa", "merge", emitter=emitter)

    statement = graph.merge_write_calls()[0].query
    repoint = statement.index("(t:Capability)-[:MERGED_INTO]->(loser)")
    assert statement.index("[:SUPPORTED_BY]") < repoint
    assert repoint < statement.index("MERGE (t)-[:MERGED_INTO]->(winner)")
    assert statement.index("MERGE (t)-[:MERGED_INTO]->(winner)") < statement.index("DETACH DELETE")
    assert re.search(
        r"MERGE \(t\)-\[:MERGED_INTO\]->\(winner\)\) WITH DISTINCT winner, loser", statement
    )


_LIVE_TEST_GRAPH = "policy_system_issue190_near_miss_tombstone_live_test"


@pytest.mark.falkordb_live
def test_loser_with_inbound_tombstone_leaves_the_tombstone_resolving_to_the_winner_live() -> None:
    """Real FalkorDB: S absorbed T earlier, then loses a near-miss to W; T must resolve to W."""
    db = connect_from_config(load_config())
    if _LIVE_TEST_GRAPH in set(db.list_graphs()):
        db.select_graph(_LIVE_TEST_GRAPH).delete()
    try:
        graph = select_graph(db, _LIVE_TEST_GRAPH)
        graph.query(
            "CREATE (:Capability {id: 'cap_w', name: 'W', status: 'active', "
            "created_at: '2020-01-01T00:00:00+00:00'}), "
            "(s:Capability {id: 'cap_s', name: 'S', status: 'active', "
            "created_at: '2024-01-01T00:00:00+00:00'}), "
            "(t:Capability {id: 'cap_t', name: 'T', status: 'merged'}), "
            "(t)-[:MERGED_INTO]->(s), "
            "(:PendingReview {id: 'review_t190', kind: 'Capability', status: 'pending', "
            "incoming_id: 'cap_s', incoming_text: 'S', nearest_existing_id: 'cap_w', "
            "nearest_existing_text: 'W', similarity: 0.9, created_at: '2024-06-01T00:00:00+00:00'})"
        )

        outcome = resolve_review(graph, "review_t190", "merge")

        assert outcome is not None
        assert (outcome.winner_id, outcome.loser_id) == ("cap_w", "cap_s")
        rows = cast(
            "list[list[object]]",
            graph.query(
                "MATCH (t:Capability {id: 'cap_t'})-[:MERGED_INTO]->(x:Capability) RETURN x.id"
            ).result_set,
        )
        assert rows == [["cap_w"]]
    finally:
        db.select_graph(_LIVE_TEST_GRAPH).delete()
