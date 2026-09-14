"""Tests for `ps_service.company_merge.pending_review.list_pending_reviews`
(issue #35, Slice 2, AC-BI-003): every unresolved `PendingReview` node is read
back from FalkorDB, mapped to a `PendingReviewRecord` field-for-field. Every
`PendingReview` node IS unresolved by construction (a resolved one is deleted
outright, never soft-status-changed -- PLAN.md §2.1/§4.2), so this exercises
the plain `MATCH (r:PendingReview) RETURN ... ORDER BY r.created_at ASC`
read, not a filtered one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ps_service.company_merge.models import PendingReviewRecord
from ps_service.company_merge.pending_review import list_pending_reviews


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
