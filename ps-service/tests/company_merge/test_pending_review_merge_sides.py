"""`read_review_merge_sides` and `choose_survivor`: one spec-driven read for every registered kind.

The review's kind is read first; its two sides are then read under that kind's own label. A side
that no longer exists makes the review stale for every kind, not only Capability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from company_merge._kinds import UNREGISTERED_KIND
from ps_service.company_merge.errors import StalePendingReviewError, UnknownReviewKindError
from ps_service.company_merge.pending_review import (
    _REVIEW_KIND_QUERY,  # pyright: ignore[reportPrivateUsage] -- the statement under test
    ReviewMergeSides,
    _sides_query,  # pyright: ignore[reportPrivateUsage] -- the statement under test
    choose_survivor,
    read_review_merge_sides,
)

if TYPE_CHECKING:
    from ps_service.company_merge.reviewable_kinds import ReviewableKind


class _Result:
    def __init__(self, rows: list[list[object]]) -> None:
        self.result_set = cast("list[object]", rows)


class _Graph:
    """Answers the review-kind read and the per-kind side read from scripted rows."""

    def __init__(
        self, kind_rows: list[list[object]], side_rows: list[list[object]] | None = None
    ) -> None:
        self._kind_rows = kind_rows
        self._side_rows = side_rows or []
        self.statements: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        self.statements.append(q)
        if q == _REVIEW_KIND_QUERY:
            return _Result(self._kind_rows)
        return _Result(self._side_rows)


def _review(kind: str) -> list[list[object]]:
    return [[kind, "node_in", "node_ex"]]


def test_a_missing_review_reads_as_none_without_a_side_read() -> None:
    graph = _Graph([])

    assert read_review_merge_sides(graph, "review_aaa") is None  # pyright: ignore[reportArgumentType] -- structural GraphHandle
    assert graph.statements == [_REVIEW_KIND_QUERY]


def test_a_review_of_an_unregistered_kind_raises_naming_it_before_any_side_read() -> None:
    graph = _Graph(_review(UNREGISTERED_KIND), [[True, True, None, None]])

    with pytest.raises(UnknownReviewKindError, match=UNREGISTERED_KIND):
        read_review_merge_sides(graph, "review_aaa")  # pyright: ignore[reportArgumentType] -- structural GraphHandle

    assert graph.statements == [_REVIEW_KIND_QUERY]


@pytest.mark.parametrize("label", ["Capability", "Role"])
def test_the_sides_are_read_under_the_reviews_own_label(label: str) -> None:
    graph = _Graph(_review(label), [[True, True, None, None]])

    read_review_merge_sides(graph, "review_aaa")  # pyright: ignore[reportArgumentType] -- structural GraphHandle

    assert graph.statements == [_REVIEW_KIND_QUERY, _sides_query(label)]
    assert f"(a:{label} {{id: r.incoming_id}})" in graph.statements[1]
    assert f"(b:{label} {{id: r.nearest_existing_id}})" in graph.statements[1]


def test_a_role_review_with_both_sides_live_reads_without_created_at() -> None:
    graph = _Graph(_review("Role"), [[True, True, None, None]])

    sides = read_review_merge_sides(graph, "review_aaa")  # pyright: ignore[reportArgumentType] -- structural GraphHandle

    assert sides == ReviewMergeSides(
        review_id="review_aaa",
        kind="Role",
        incoming_id="node_in",
        existing_id="node_ex",
        incoming_created_at=None,
        existing_created_at=None,
    )


def test_a_role_has_no_created_at_so_the_incoming_role_survives() -> None:
    graph = _Graph(_review("Role"), [[True, True, None, None]])
    sides = read_review_merge_sides(graph, "review_aaa")  # pyright: ignore[reportArgumentType] -- structural GraphHandle
    assert sides is not None

    assert choose_survivor(sides) == ("node_in", "node_ex")


@pytest.mark.parametrize(
    ("label", "side_row"),
    [
        ("Capability", [False, True, None, "2026-01-01"]),
        ("Capability", [True, False, "2026-01-01", None]),
        ("Role", [False, True, None, None]),
        ("Role", [True, False, None, None]),
    ],
)
def test_a_review_whose_side_was_absorbed_is_stale_for_every_kind(
    label: str, side_row: list[object]
) -> None:
    graph = _Graph(_review(label), [side_row])

    with pytest.raises(StalePendingReviewError):
        read_review_merge_sides(graph, "review_aaa")  # pyright: ignore[reportArgumentType] -- structural GraphHandle


def test_a_test_only_kind_review_is_read_under_its_label_and_checked_for_staleness(
    test_only_reviewable_kind: ReviewableKind,
) -> None:
    label = test_only_reviewable_kind.label
    live = _Graph(_review(label), [[True, True, None, None]])
    gone = _Graph(_review(label), [[False, False, None, None]])

    sides = read_review_merge_sides(live, "review_aaa")  # pyright: ignore[reportArgumentType] -- structural GraphHandle

    assert sides is not None
    assert (sides.kind, sides.incoming_id, sides.existing_id) == (label, "node_in", "node_ex")
    with pytest.raises(StalePendingReviewError):
        read_review_merge_sides(gone, "review_aaa")  # pyright: ignore[reportArgumentType] -- structural GraphHandle


def test_capability_survivor_is_still_the_earlier_created_at() -> None:
    older_incoming = ReviewMergeSides("r", "Capability", "in", "ex", "2020-01-01", "2021-01-01")
    older_existing = ReviewMergeSides("r", "Capability", "in", "ex", "2022-01-01", "2021-01-01")

    assert choose_survivor(older_incoming) == ("in", "ex")
    assert choose_survivor(older_existing) == ("ex", "in")
