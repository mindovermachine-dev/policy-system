"""Tests for `ps_service.company_merge.pending_review.resolve_review`
(issue #35): AC-BI-004 (keep-separate deletes only the `PendingReview`
record), AC-BI-005/006/007 (merge re-points edges, deletes the loser and the
review, atomically, with deterministic winner selection), AC-BI-008 (both
decisions' not-found paths make no graph changes at all -- including merge's
own "stale reference" case, CHANGES.md H2), and AC-BI-009 (a structured log
entry records the decision, review id, and timestamp -- plus winner/loser
for merge).

Mirrors `test_pending_review_list.py`'s `_FakeGraph`/`_FakeQueryResult`
fakes -- structural stand-ins for `GraphHandle`/`GraphQueryResult`, no
mocking library (PLAN.md §0.6's established convention). These scripted
tests can only prove "this query string/these params were sent and this
scripted return value was mapped onto `ResolveOutcome` correctly" -- NOT
that the Cypher itself does what it says (a fake cannot execute Cypher).
The merge query's actual correctness -- the `WITH DISTINCT winner, loser`
C1 fix, the `coalesce(created_at, '')` M2 winner-selection convention, and
FOREACH/CASE support itself -- is proven by the required
`falkordb_live`-marked test in this same file, against a real FalkorDB
instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.company_merge.errors import StalePendingReviewError
from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.company_merge.models import ResolveOutcome
from ps_service.company_merge.pending_review import resolve_review
from ps_service.config import load_config

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    """Satisfies `GraphHandle` structurally; returns one scripted result per call, in order."""

    def __init__(self, results: list[list[list[object]]]) -> None:
        self.calls: list[_RecordedCall] = []
        self._results = list(results)

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        rows = self._results.pop(0) if self._results else []
        return _FakeQueryResult(cast("list[object]", rows))


def test_resolve_keep_separate_deletes_review_only(make_emitter: MakeEmitter) -> None:
    """AC-BI-004: the PendingReview record is removed, no other graph node/edge changes."""
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(results=[[["Capability"]], []])

    outcome = resolve_review(graph, "review_aaa", "keep-separate", emitter=emitter)

    assert outcome == ResolveOutcome(
        review_id="review_aaa", decision="keep-separate", winner_id=None, loser_id=None
    )
    assert len(graph.calls) == 2
    assert "MATCH (r:PendingReview {id: $review_id}) RETURN" in graph.calls[0].query
    assert graph.calls[0].params == {"review_id": "review_aaa"}
    assert "DELETE r" in graph.calls[1].query
    assert graph.calls[1].params == {"review_id": "review_aaa"}


def test_resolve_nonexistent_id_returns_none_before_any_write() -> None:
    """AC-BI-008 (not-found half): an unknown/already-resolved id makes no graph changes."""
    graph = _FakeGraph(results=[[]])

    outcome = resolve_review(graph, "review_missing", "keep-separate")

    assert outcome is None
    assert len(graph.calls) == 1
    assert "MATCH (r:PendingReview {id: $review_id}) RETURN" in graph.calls[0].query
    assert graph.calls[0].params == {"review_id": "review_missing"}


def test_resolve_keep_separate_emits_structured_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-009 (keep-separate half): decision, review id, and timestamp are logged."""
    emitter, log_path = make_emitter()
    graph = _FakeGraph(results=[[["Capability"]], []])

    resolve_review(graph, "review_aaa", "keep-separate", emitter=emitter)
    emitter.flush()

    entries = read_lines(log_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["component"] == "company_merge"
    assert entry["action"] == "resolve_near_miss_review"
    assert entry["entity_id"] == "review_aaa"
    assert entry["outcome"] == "keep-separate"
    assert "timestamp" in entry


def test_resolve_nonexistent_id_emits_no_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """No log noise for an id that was never resolved -- nothing happened to log."""
    emitter, log_path = make_emitter()
    graph = _FakeGraph(results=[[]])

    resolve_review(graph, "review_missing", "keep-separate", emitter=emitter)
    emitter.flush()

    assert read_lines(log_path) == []


# --- decision="merge" (issue #35, Slice 4: AC-BI-005/006/007, AC-BI-008/009 completed) ---


def _merge_results(
    *,
    kind: str = "Capability",
    incoming_id: str = "cap_incoming",
    nearest_existing_id: str = "cap_existing",
    incoming_exists: bool = True,
    existing_exists: bool = True,
    winner_id: str = "cap_existing",
    loser_id: str = "cap_incoming",
) -> list[list[list[object]]]:
    """Build a 3-call scripted result sequence for a happy-path merge resolve."""
    return [
        [[kind]],
        [[incoming_id, nearest_existing_id, incoming_exists, existing_exists]],
        [[winner_id, loser_id]],
    ]


def test_resolve_merge_repoints_edges_and_deletes_loser_scripted(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-005/006/007: the full 3-query merge sequence, params, and outcome mapping."""
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(results=_merge_results())

    outcome = resolve_review(graph, "review_aaa", "merge", emitter=emitter)

    assert outcome == ResolveOutcome(
        review_id="review_aaa", decision="merge", winner_id="cap_existing", loser_id="cap_incoming"
    )
    assert len(graph.calls) == 3

    # Step 1: shared "does the review exist" read (identical to keep-separate's own Step 1).
    assert "MATCH (r:PendingReview {id: $review_id}) RETURN" in graph.calls[0].query
    assert graph.calls[0].params == {"review_id": "review_aaa"}

    # Step 2: H2's combined existence-check read, kind-templated.
    existence_query = graph.calls[1].query
    assert ":Capability {id: r.incoming_id}" in existence_query
    assert ":Capability {id: r.nearest_existing_id}" in existence_query
    assert "incoming_exists" in existence_query
    assert "existing_exists" in existence_query
    assert graph.calls[1].params == {"review_id": "review_aaa"}

    # Step 3: the atomic merge write (Appendix C1), kind-templated, ids passed as params.
    merge_query = graph.calls[2].query
    assert ":Capability {id: $incoming_id}" in merge_query
    assert ":Capability {id: $nearest_existing_id}" in merge_query
    assert "DETACH DELETE loser, rev" in merge_query
    assert graph.calls[2].params == {
        "incoming_id": "cap_incoming",
        "nearest_existing_id": "cap_existing",
        "review_id": "review_aaa",
    }


def test_resolve_merge_query_has_distinct_after_every_foreach_c1_fix(
    make_emitter: MakeEmitter,
) -> None:
    """CHANGES.md C1 regression guard: `WITH DISTINCT winner, loser` after every FOREACH block.

    Without this, `DETACH DELETE loser, rev` would run once per surviving
    row instead of once whenever the loser has more than one matching edge
    (e.g. >=2 Obligations REQUIRES-ing the same loser Capability -- the
    domain's primary scenario, not an edge case). Exactly 4 FOREACH blocks
    (REQUIRES, GOVERNED_BY from loser, GOVERNED_BY to loser, SUPPORTED_BY)
    means exactly 4 `WITH DISTINCT winner, loser` occurrences.
    """
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(results=_merge_results())

    resolve_review(graph, "review_aaa", "merge", emitter=emitter)

    merge_query = graph.calls[2].query
    assert merge_query.count("FOREACH") == 4
    assert merge_query.count("WITH DISTINCT winner, loser") == 4
    # No plain, un-DISTINCT `WITH winner, loser` should remain between blocks.
    assert "WITH winner, loser" not in merge_query.replace("WITH DISTINCT winner, loser", "")


def test_resolve_merge_query_embeds_coalesce_null_created_at_convention(
    make_emitter: MakeEmitter,
) -> None:
    """M2 (final, user-confirmed): `coalesce(created_at, '') <=` -- NULL/absent always wins."""
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(results=_merge_results())

    resolve_review(graph, "review_aaa", "merge", emitter=emitter)

    merge_query = graph.calls[2].query
    assert "coalesce(a.created_at, '') <= coalesce(b.created_at, '')" in merge_query


def test_resolve_merge_uses_policy_label_when_kind_is_policy(make_emitter: MakeEmitter) -> None:
    """The `{kind}` template substitutes `Policy`, not `Capability`, for a Policy review.

    The merge write query (Appendix C1) is a single universal query covering
    all four possible edge types unconditionally (`Capability`/`Standard`
    legitimately still appear in the unrelated GOVERNED_BY/SUPPORTED_BY
    blocks, which simply no-op for the wrong kind at the real-FalkorDB
    level -- PLAN.md §4.3's own "style choice, not load-bearing" note) --
    only the two `{kind}`-templated MATCH clauses that select `a`/`b`
    themselves change.
    """
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(
        results=_merge_results(
            kind="Policy",
            incoming_id="pol_incoming",
            nearest_existing_id="pol_existing",
            winner_id="pol_existing",
            loser_id="pol_incoming",
        )
    )

    outcome = resolve_review(graph, "review_aaa", "merge", emitter=emitter)

    assert outcome == ResolveOutcome(
        review_id="review_aaa", decision="merge", winner_id="pol_existing", loser_id="pol_incoming"
    )
    assert ":Policy {id: r.incoming_id}" in graph.calls[1].query
    assert graph.calls[2].query.startswith(
        "MATCH (a:Policy {id: $incoming_id}), (b:Policy {id: $nearest_existing_id})"
    )


@pytest.mark.parametrize(
    ("winner_id", "loser_id"),
    [("cap_a", "cap_b"), ("cap_b", "cap_a")],
)
def test_resolve_merge_maps_returned_winner_and_loser_onto_outcome(
    winner_id: str, loser_id: str, make_emitter: MakeEmitter
) -> None:
    """Exit criterion #2: whatever the (scripted) query returns maps straight onto ResolveOutcome.

    `resolve_review` itself performs no client-side created_at comparison --
    winner/loser selection is entirely the merge query's own job (proven
    live); this only proves the Python-side read-back is a faithful,
    order-preserving passthrough in both directions.
    """
    emitter, _log_path = make_emitter()
    graph = _FakeGraph(
        results=_merge_results(
            incoming_id="cap_a", nearest_existing_id="cap_b", winner_id=winner_id, loser_id=loser_id
        )
    )

    outcome = resolve_review(graph, "review_aaa", "merge", emitter=emitter)

    assert outcome == ResolveOutcome(
        review_id="review_aaa", decision="merge", winner_id=winner_id, loser_id=loser_id
    )
    assert graph.calls[2].params == {
        "incoming_id": "cap_a",
        "nearest_existing_id": "cap_b",
        "review_id": "review_aaa",
    }


def test_resolve_merge_of_nonexistent_review_returns_none_before_any_write() -> None:
    """AC-BI-008 (not-found half, merge): an unknown/already-resolved id makes no graph changes."""
    graph = _FakeGraph(results=[[]])

    outcome = resolve_review(graph, "review_missing", "merge")

    assert outcome is None
    assert len(graph.calls) == 1


def test_resolve_merge_when_review_vanishes_before_existence_check_returns_none() -> None:
    """Benign race: Step 1 found the review, but it's gone by Step 2 -- treated as not-found."""
    graph = _FakeGraph(results=[[["Capability"]], []])

    outcome = resolve_review(graph, "review_aaa", "merge")

    assert outcome is None
    assert len(graph.calls) == 2


def test_resolve_merge_with_stale_incoming_node_raises_stale_error() -> None:
    """CHANGES.md H2: `incoming_id` no longer resolves -- stale, no merge write issued."""
    graph = _FakeGraph(results=[[["Capability"]], [["cap_incoming", "cap_existing", False, True]]])

    with pytest.raises(StalePendingReviewError) as excinfo:
        resolve_review(graph, "review_aaa", "merge")

    assert excinfo.value.review_id == "review_aaa"
    assert len(graph.calls) == 2  # existence check only -- the merge write never ran


def test_resolve_merge_with_stale_existing_node_raises_stale_error() -> None:
    """CHANGES.md H2: `nearest_existing_id` no longer resolves -- stale, no merge write issued."""
    graph = _FakeGraph(results=[[["Capability"]], [["cap_incoming", "cap_existing", True, False]]])

    with pytest.raises(StalePendingReviewError) as excinfo:
        resolve_review(graph, "review_aaa", "merge")

    assert excinfo.value.review_id == "review_aaa"
    assert len(graph.calls) == 2


def test_resolve_merge_stale_reference_emits_no_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """No log noise for a stale reference -- no merge actually happened."""
    emitter, log_path = make_emitter()
    graph = _FakeGraph(results=[[["Capability"]], [["cap_incoming", "cap_existing", False, True]]])

    with pytest.raises(StalePendingReviewError):
        resolve_review(graph, "review_aaa", "merge", emitter=emitter)
    emitter.flush()

    assert read_lines(log_path) == []


def test_resolve_merge_emits_structured_log_entry_with_winner_and_loser(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-009 (full): merge logs decision, review id, timestamp, AND winner/loser ids."""
    emitter, log_path = make_emitter()
    graph = _FakeGraph(results=_merge_results())

    resolve_review(graph, "review_aaa", "merge", emitter=emitter)
    emitter.flush()

    entries = read_lines(log_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["component"] == "company_merge"
    assert entry["action"] == "resolve_near_miss_review"
    assert entry["entity_id"] == "review_aaa"
    assert entry["outcome"] == "merge"
    assert entry["winner_id"] == "cap_existing"
    assert entry["loser_id"] == "cap_incoming"
    assert "timestamp" in entry


# --- falkordb_live (issue #35, Slice 4, CHANGES.md H1: REQUIRED, not optional) ---

_LIVE_TEST_GRAPH = "policy_system_slice4_merge_live_test"


@pytest.mark.falkordb_live
def test_resolve_merge_repoints_edges_and_deletes_loser_live(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-005/006/007 against a REAL FalkorDB instance (CHANGES.md H1's required gate).

    This is the one test in this file that can actually prove the C1
    `WITH DISTINCT winner, loser` fix works -- a scripted fake cannot
    execute Cypher, so it can never prove the FOREACH/CASE idiom is
    supported or that the row-multiplication bug is actually fixed.

    Writes into a dedicated, disposable graph name
    (`policy_system_slice4_merge_live_test`), NEVER the real, shared
    `policy_system` graph -- mirrors `test_baseline_graph_isolation.py`'s
    and `test_live_capstone.py`'s own established "a database that already
    holds real regulatory data is not something this test mutates without a
    human's separately-obtained permission" convention (CLAUDE.md). The
    disposable graph is deleted in a `finally` block regardless of outcome,
    including a failing run, so no live residue survives (per this slice's
    own "leave the real FalkorDB instance clean" requirement) -- a
    defensive pre-clean also runs first, in case a prior failed run left
    residue.

    Seeds TWO `Obligation`s `REQUIRES`-ing the same loser `Capability`
    (mirroring `docs/artifacts/ps-domain-concepts.md:652-681`'s worked
    example -- the domain's *primary* convergence scenario, not an edge
    case) plus a `Policy` the loser is `GOVERNED_BY`, so the merge query's
    `OPTIONAL MATCH (o:Obligation)-[:REQUIRES]->(loser)` genuinely produces
    2 rows before `WITH DISTINCT winner, loser` collapses them back to 1 --
    exactly the condition CHANGES.md C1 identified as triggering a repeated
    `DETACH DELETE loser, rev` (once per surviving row) without the fix.
    If FOREACH/CASE had proven unsupported by real FalkorDB, this test
    would have failed with a Cypher syntax/semantic error unrelated to
    C1 -- that did not happen: FOREACH/CASE worked as-is once `DISTINCT`
    was added, so CHANGES.md Appendix H1's UNWIND fallback was not needed.
    """
    db = connect_from_config(load_config())

    existing_graphs = set(db.list_graphs())
    if _LIVE_TEST_GRAPH in existing_graphs:
        db.select_graph(_LIVE_TEST_GRAPH).delete()

    winner_id = "cap_winner_live_slice4"
    loser_id = "cap_loser_live_slice4"
    obligation_a_id = "obl_a_live_slice4"
    obligation_b_id = "obl_b_live_slice4"
    policy_id = "pol_live_slice4"
    review_id = "review_live_slice4_merge"

    emitter, log_path = make_emitter()
    try:
        graph = select_graph(db, _LIVE_TEST_GRAPH)

        # Winner: earlier created_at (2020). Loser: later created_at (2024). M2: earlier wins.
        graph.query(
            "CREATE (:Capability {id: $id, name: 'Incident Notification (winner)', "
            "created_at: '2020-01-01T00:00:00+00:00'})",
            params={"id": winner_id},
        )
        graph.query(
            "CREATE (:Capability {id: $id, name: 'Incident Notification (loser)', "
            "created_at: '2024-01-01T00:00:00+00:00'})",
            params={"id": loser_id},
        )
        # Two Obligations REQUIRES-ing the loser -- the multi-edge, row-multiplication scenario.
        graph.query(
            "MATCH (c:Capability {id: $loser_id}) "
            "CREATE (:Obligation {id: $obligation_id})-[:REQUIRES]->(c)",
            params={"loser_id": loser_id, "obligation_id": obligation_a_id},
        )
        graph.query(
            "MATCH (c:Capability {id: $loser_id}) "
            "CREATE (:Obligation {id: $obligation_id})-[:REQUIRES]->(c)",
            params={"loser_id": loser_id, "obligation_id": obligation_b_id},
        )
        # The loser GOVERNED_BY a Policy -- the second edge type C1's query re-points.
        graph.query(
            "MATCH (c:Capability {id: $loser_id}) "
            "CREATE (c)-[:GOVERNED_BY]->(:Policy {id: $policy_id, name: 'Live Policy'})",
            params={"loser_id": loser_id, "policy_id": policy_id},
        )
        graph.query(
            "CREATE (:PendingReview {id: $id, kind: 'Capability', status: 'pending', "
            "incoming_id: $incoming_id, incoming_text: 'incoming near-miss text', "
            "nearest_existing_id: $nearest_existing_id, "
            "nearest_existing_text: 'existing near-miss text', "
            "similarity: 0.91, created_at: '2024-06-01T00:00:00+00:00'})",
            params={"id": review_id, "incoming_id": loser_id, "nearest_existing_id": winner_id},
        )

        # The actual call under test: the real resolve_review, against real FalkorDB.
        outcome = resolve_review(graph, review_id, "merge", emitter=emitter)
        emitter.flush()

        assert outcome is not None, "resolve_review returned None -- review/nodes not found"
        assert outcome.review_id == review_id
        assert outcome.decision == "merge"
        assert outcome.winner_id == winner_id
        assert outcome.loser_id == loser_id

        # Both Obligations now REQUIRES the winner (not the deleted loser).
        obligations_result = graph.query(
            "MATCH (o:Obligation)-[:REQUIRES]->(c:Capability {id: $winner_id}) "
            "RETURN o.id ORDER BY o.id",
            params={"winner_id": winner_id},
        )
        obligation_rows = cast("list[list[object]]", obligations_result.result_set)
        assert sorted(cast("str", row[0]) for row in obligation_rows) == sorted(
            [obligation_a_id, obligation_b_id]
        )

        # No duplicate REQUIRES edges were left behind (DISTINCT collapsed row multiplication).
        requires_count_result = graph.query(
            "MATCH (:Obligation)-[r:REQUIRES]->(:Capability {id: $winner_id}) RETURN count(r)",
            params={"winner_id": winner_id},
        )
        requires_count_rows = cast("list[list[object]]", requires_count_result.result_set)
        assert requires_count_rows[0][0] == 2

        # The winner is now GOVERNED_BY the same Policy the loser used to be.
        governed_by_result = graph.query(
            "MATCH (c:Capability {id: $winner_id})-[:GOVERNED_BY]->(p:Policy) RETURN p.id",
            params={"winner_id": winner_id},
        )
        governed_by_rows = cast("list[list[object]]", governed_by_result.result_set)
        assert [cast("str", row[0]) for row in governed_by_rows] == [policy_id]

        # The loser node no longer exists.
        loser_result = graph.query(
            "MATCH (c:Capability {id: $loser_id}) RETURN c", params={"loser_id": loser_id}
        )
        assert loser_result.result_set == []

        # The PendingReview node no longer exists.
        review_result = graph.query(
            "MATCH (r:PendingReview {id: $review_id}) RETURN r", params={"review_id": review_id}
        )
        assert review_result.result_set == []

        # AC-BI-009 (full): the structured log entry names winner/loser too.
        entries = read_lines(log_path)
        assert len(entries) == 1
        assert entries[0]["component"] == "company_merge"
        assert entries[0]["action"] == "resolve_near_miss_review"
        assert entries[0]["entity_id"] == review_id
        assert entries[0]["outcome"] == "merge"
        assert entries[0]["winner_id"] == winner_id
        assert entries[0]["loser_id"] == loser_id
    finally:
        db.select_graph(_LIVE_TEST_GRAPH).delete()
        remaining_graphs = set(db.list_graphs())
        assert _LIVE_TEST_GRAPH not in remaining_graphs
