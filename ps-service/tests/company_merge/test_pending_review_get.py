"""`pending_review.get_pending_review` (issue #195): a single review by id, WITHOUT the resolvable
predicate of `list_pending_reviews`, so a stale review still yields its entity ids for auditing.
"""

from __future__ import annotations

from typing import cast

from ps_service.company_merge.models import PendingReviewRecord
from ps_service.company_merge.pending_review import get_pending_review

_ROW: list[object] = [
    "review_aaa",
    "Capability",
    "capability_incoming_a",
    "Report the incident.",
    "capability_existing_a",
    "Assess risk.",
    0.62,
    "2026-01-01T00:00:00+00:00",
]


class _Result:
    def __init__(self, rows: list[list[object]]) -> None:
        self.result_set = cast("list[object]", rows)


class _Graph:
    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.calls.append((q, params))
        return _Result(self.rows)


def test_get_pending_review_maps_the_row_field_for_field() -> None:
    graph = _Graph([_ROW])

    record = get_pending_review(graph, "review_aaa")  # pyright: ignore[reportArgumentType]  -- structural GraphHandle

    assert record == PendingReviewRecord(
        id="review_aaa",
        kind="Capability",
        incoming_id="capability_incoming_a",
        incoming_text="Report the incident.",
        nearest_existing_id="capability_existing_a",
        nearest_existing_text="Assess risk.",
        similarity=0.62,
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert graph.calls[0][1] == {"review_id": "review_aaa"}


def test_get_pending_review_returns_none_when_no_such_review() -> None:
    assert get_pending_review(_Graph([]), "missing") is None  # pyright: ignore[reportArgumentType]  -- structural GraphHandle


def test_get_pending_review_query_has_no_resolvable_predicate() -> None:
    graph = _Graph([_ROW])

    get_pending_review(graph, "review_aaa")  # pyright: ignore[reportArgumentType]  -- structural GraphHandle

    assert "merged" not in graph.calls[0][0]
