"""Tests for `ps_service.company_merge.pending_review.list_pending_reviews`
(issue #35, Slice 2, AC-BI-003): every unresolved `PendingReview` node is read
back from FalkorDB, mapped to a `PendingReviewRecord` field-for-field. Every
`PendingReview` node IS unresolved by construction (a resolved one is deleted
outright, never soft-status-changed -- PLAN.md §2.1/§4.2).

Since issue #196 (Cause 8) the read is nevertheless a *filtered* one: being
unresolved is not the same as being resolvable, so the query also requires
both referenced nodes to still exist under the review's own `kind` and
neither to be a `merged` tombstone -- the same predicate the merge path
enforces. The scripted tests below cannot prove that filtering works (a fake
cannot execute Cypher); the `falkordb_live` test at the end of this file does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from ps_service.company_merge.falkordb_client import connect_from_config, select_graph
from ps_service.company_merge.models import PendingReviewRecord
from ps_service.company_merge.pending_review import list_pending_reviews
from ps_service.config import load_config


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
    """Satisfies `GraphHandle` structurally, recording every call and returning scripted rows."""

    def __init__(self, rows: list[list[object]] | None = None) -> None:
        self.calls: list[_RecordedCall] = []
        self._rows = rows or []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult(cast("list[object]", self._rows))


_ROW_A: list[object] = [
    "review_aaa",
    "Capability",
    "capability_incoming_a",
    "Report the incident to the authority.",
    "capability_existing_a",
    "Conduct a risk assessment.",
    0.62,
    "2026-01-01T00:00:00+00:00",
]
_ROW_B: list[object] = [
    "review_bbb",
    "Policy",
    "policy_incoming_b",
    "Maintain a data protection policy.",
    "policy_existing_b",
    "Maintain a privacy policy.",
    0.7,
    "2026-01-02T00:00:00+00:00",
]


def test_list_pending_reviews_no_nodes_returns_empty_tuple() -> None:
    graph = _FakeGraph(rows=[])

    result = list_pending_reviews(graph)

    assert result == ()
    assert len(graph.calls) == 1
    assert "MATCH (r:PendingReview)" in graph.calls[0].query


def test_list_pending_reviews_maps_every_field_verbatim() -> None:
    graph = _FakeGraph(rows=[_ROW_A])

    result = list_pending_reviews(graph)

    assert result == (
        PendingReviewRecord(
            id="review_aaa",
            kind="Capability",
            incoming_id="capability_incoming_a",
            incoming_text="Report the incident to the authority.",
            nearest_existing_id="capability_existing_a",
            nearest_existing_text="Conduct a risk assessment.",
            similarity=0.62,
            created_at="2026-01-01T00:00:00+00:00",
        ),
    )


def test_list_pending_reviews_returns_one_record_per_node_in_query_order() -> None:
    graph = _FakeGraph(rows=[_ROW_A, _ROW_B])

    result = list_pending_reviews(graph)

    assert len(result) == 2
    assert result[0].id == "review_aaa"
    assert result[1].id == "review_bbb"
    assert result[1].kind == "Policy"


def test_list_pending_reviews_orders_by_created_at_ascending_in_the_query() -> None:
    graph = _FakeGraph(rows=[])

    list_pending_reviews(graph)

    assert "ORDER BY r.created_at ASC" in graph.calls[0].query


# --- falkordb_live (issue #196, Cause 8): the list and the merge must agree ---------

_LIVE_TEST_GRAPH = "policy_system_issue196_stale_review_live_test"


@pytest.mark.falkordb_live
def test_list_pending_reviews_omits_reviews_the_merge_path_would_refuse_live() -> None:
    """Issue #196 Cause 8, against a REAL FalkorDB instance.

    `list_pending_reviews` applied no predicate at all while
    `_MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE` requires both referenced nodes to
    exist *and* not be a `merged` tombstone. A review only the list accepted
    was therefore offered to a Compliance Officer, consumed a passkey
    approval, raised `StalePendingReviewError` at sign time -- and, because
    the stale path deliberately makes no graph changes, was never retired.
    It stayed listed forever and every retry failed identically.

    Only a live instance can prove this: a scripted `_FakeGraph` cannot
    execute Cypher, so it can neither show the old query over-reporting nor
    that `labels()`/`coalesce(status, 'active')` behave as assumed here.

    Writes into a dedicated, disposable graph name, NEVER the real shared
    `policy_system` graph (nor any `*_baseline`/`*_native`) -- mirrors this
    package's established convention, with the same defensive pre-clean and
    a `finally` delete so no residue survives a failing run.
    """
    db = connect_from_config(load_config())
    if _LIVE_TEST_GRAPH in set(db.list_graphs()):
        db.select_graph(_LIVE_TEST_GRAPH).delete()

    try:
        graph = select_graph(db, _LIVE_TEST_GRAPH)
        graph.query("CREATE (:Capability {id: 'cap_live_a', status: 'active'})")
        # No `status` property at all -- the coalesce(…, 'active') default path.
        graph.query("CREATE (:Capability {id: 'cap_live_b'})")
        graph.query("CREATE (:Capability {id: 'cap_live_merged', status: 'merged'})")
        graph.query("CREATE (:Policy {id: 'pol_live_a'})")
        graph.query("CREATE (:Policy {id: 'pol_live_b'})")

        for review_id, kind, incoming_id, nearest_id, created_at in (
            ("review_live_resolvable", "Capability", "cap_live_a", "cap_live_b", "2024-01-01"),
            ("review_live_node_gone", "Capability", "cap_live_absent", "cap_live_b", "2024-01-02"),
            (
                "review_live_merged_side",
                "Capability",
                "cap_live_a",
                "cap_live_merged",
                "2024-01-03",
            ),
            ("review_live_policy_ok", "Policy", "pol_live_a", "pol_live_b", "2024-01-04"),
            # Nodes with these ids exist, but as Policy, not Capability: the
            # review's own `kind` must decide, or a merge would target a label
            # the existence check never approved.
            ("review_live_wrong_kind", "Capability", "pol_live_a", "pol_live_b", "2024-01-05"),
        ):
            graph.query(
                "CREATE (:PendingReview {id: $id, kind: $kind, incoming_id: $incoming_id, "
                "incoming_text: 'incoming', nearest_existing_id: $nearest_id, "
                "nearest_existing_text: 'nearest', similarity: 0.62, created_at: $created_at})",
                params={
                    "id": review_id,
                    "kind": kind,
                    "incoming_id": incoming_id,
                    "nearest_id": nearest_id,
                    "created_at": created_at,
                },
            )

        listed = [record.id for record in list_pending_reviews(graph)]

        assert listed == ["review_live_resolvable", "review_live_policy_ok"]
    finally:
        db.select_graph(_LIVE_TEST_GRAPH).delete()
        assert _LIVE_TEST_GRAPH not in set(db.list_graphs())
