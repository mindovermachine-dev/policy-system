"""`pending_review.get_pending_review`: a single review by id, WITHOUT the resolvable
predicate of `list_pending_reviews`, so a stale review still yields its entity ids for auditing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from company_merge._kinds import UNREGISTERED_KIND
from ps_service.company_merge.errors import UnknownReviewKindError
from ps_service.company_merge.models import PendingReviewRecord
from ps_service.company_merge.pending_review import get_pending_review, read_review_merge_sides

if TYPE_CHECKING:
    from ps_service.company_merge.reviewable_kinds import ReviewableKind

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


def test_get_pending_review_rejects_a_review_whose_kind_has_no_spec_naming_the_kind() -> None:
    row = [*_ROW]
    row[1] = UNREGISTERED_KIND

    with pytest.raises(UnknownReviewKindError, match=UNREGISTERED_KIND):
        get_pending_review(_Graph([row]), "review_aaa")  # pyright: ignore[reportArgumentType]  -- structural GraphHandle


_SIDES_ROW_HEAD = ["cap_in", "cap_ex", True, True, "2020-01-01", "2021-01-01"]


def test_merge_sides_of_a_review_with_an_unknown_kind_raise_naming_it() -> None:
    graph = _Graph([[UNREGISTERED_KIND, *_SIDES_ROW_HEAD]])

    with pytest.raises(UnknownReviewKindError, match=UNREGISTERED_KIND):
        read_review_merge_sides(graph, "review_aaa")  # pyright: ignore[reportArgumentType]  -- structural GraphHandle


def test_merge_sides_of_a_test_only_kind_review_are_read_without_a_staleness_check(
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    graph = _Graph([[test_only_reviewable_kind.label, "p_in", "p_ex", False, False, None, None]])

    sides = read_review_merge_sides(graph, "review_aaa")  # pyright: ignore[reportArgumentType]  -- structural GraphHandle

    assert sides is not None
    assert (sides.kind, sides.incoming_id, sides.existing_id) == ("ProbeItem", "p_in", "p_ex")
