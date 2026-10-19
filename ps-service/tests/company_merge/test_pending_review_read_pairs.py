"""`read_unresolved_review_pairs`: which near-miss pairs already have a `PendingReview`."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from company_merge._kinds import UNREGISTERED_KIND
from ps_service.company_merge.errors import UnknownReviewKindError
from ps_service.company_merge.models import NearMissPair
from ps_service.company_merge.pending_review import read_unresolved_review_pairs
from ps_service.company_merge.reviewable_kinds import CAPABILITY, ReviewableKind


@dataclass
class _Result:
    result_set: list[list[object]]


@dataclass
class _Graph:
    rows: list[list[object]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        self.calls.append((q, dict(params or {})))
        return _Result(self.rows)


def _pair(incoming: str, nearest: str) -> NearMissPair:
    return NearMissPair(
        incoming_id=incoming,
        incoming_text=f"text {incoming}",
        nearest_existing_id=nearest,
        nearest_existing_text=f"text {nearest}",
        similarity=0.8,
    )


def test_no_near_misses_issue_no_query() -> None:
    graph = _Graph()

    assert read_unresolved_review_pairs(graph, (), kind=CAPABILITY) == set()  # pyright: ignore[reportArgumentType]
    assert graph.calls == []


def test_one_parameterised_read_returns_the_pairs_found() -> None:
    graph = _Graph(rows=[["a", "x"]])

    found = read_unresolved_review_pairs(
        graph,  # pyright: ignore[reportArgumentType]
        (_pair("a", "x"), _pair("b", "y"), _pair("a", "x")),
        kind=CAPABILITY,
    )

    assert found == {("a", "x")}
    ((query, params),) = graph.calls
    assert "PendingReview" in query
    assert "$pairs" in query
    assert "text a" not in query
    assert params["kind"] == "Capability"
    assert params["pairs"] == [
        {"incoming_id": "a", "nearest_existing_id": "x"},
        {"incoming_id": "b", "nearest_existing_id": "y"},
    ]


def test_an_unregistered_kind_is_rejected_naming_it_before_any_read() -> None:
    graph = _Graph()
    unregistered = ReviewableKind(UNREGISTERED_KIND, "name", "global", "merge-above")

    with pytest.raises(UnknownReviewKindError, match=UNREGISTERED_KIND):
        read_unresolved_review_pairs(graph, (_pair("a", "x"),), kind=unregistered)  # pyright: ignore[reportArgumentType]

    assert graph.calls == []
