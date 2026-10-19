"""`drop_already_reviewed`: the near misses that do not yet have an unresolved review.

Ingest (`merge_baseline_graph`) and restore both call this one helper, so a pair a review already
names is never recorded a second time. Matching is on the ordered pair
`(incoming_id, nearest_existing_id)` within one kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from company_merge._kinds import UNREGISTERED_KIND
from ps_service.company_merge.errors import UnknownReviewKindError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import NearMissPair
from ps_service.company_merge.pending_review import drop_already_reviewed, persist_pending_reviews
from ps_service.company_merge.reviewable_kinds import CAPABILITY, ReviewableKind

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig


def _pair(incoming: str, nearest: str) -> NearMissPair:
    return NearMissPair(
        incoming_id=incoming,
        incoming_text=f"text {incoming}",
        nearest_existing_id=nearest,
        nearest_existing_text=f"text {nearest}",
        similarity=0.8,
    )


@dataclass
class _Result:
    result_set: list[list[object]]


@dataclass
class _CountingGraph:
    calls: list[str] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        self.calls.append(q)
        return _Result([])


def _persist(rig: MergeRig, kind: ReviewableKind, *pairs: NearMissPair) -> None:
    persist_pending_reviews(
        GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id),
        pairs,
        kind=kind,
        values=stepped_values(),
    )


def test_pairs_with_an_unresolved_review_are_dropped(merge_rig: MergeRig) -> None:
    reviewed, fresh = _pair("a", "x"), _pair("b", "y")
    _persist(merge_rig, CAPABILITY, reviewed)

    kept = drop_already_reviewed(merge_rig.legacy, (reviewed, fresh), kind=CAPABILITY)

    assert kept == (fresh,)


def test_the_order_of_the_kept_pairs_is_the_input_order(merge_rig: MergeRig) -> None:
    reviewed = _pair("b", "y")
    _persist(merge_rig, CAPABILITY, reviewed)
    pairs = (_pair("c", "z"), reviewed, _pair("a", "x"))

    kept = drop_already_reviewed(merge_rig.legacy, pairs, kind=CAPABILITY)

    assert kept == (pairs[0], pairs[2])


def test_a_reversed_pair_is_not_a_duplicate(merge_rig: MergeRig) -> None:
    _persist(merge_rig, CAPABILITY, _pair("a", "x"))
    reversed_pair = _pair("x", "a")

    assert drop_already_reviewed(merge_rig.legacy, (reversed_pair,), kind=CAPABILITY) == (
        reversed_pair,
    )


def test_nothing_is_read_for_no_near_misses() -> None:
    graph = _CountingGraph()

    assert drop_already_reviewed(graph, (), kind=CAPABILITY) == ()  # pyright: ignore[reportArgumentType]
    assert graph.calls == []


def test_only_reviews_of_the_given_kind_count(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    pair = _pair("a", "x")
    _persist(merge_rig, test_only_reviewable_kind, pair)

    assert drop_already_reviewed(merge_rig.legacy, (pair,), kind=CAPABILITY) == (pair,)
    assert drop_already_reviewed(merge_rig.legacy, (pair,), kind=test_only_reviewable_kind) == ()


def test_an_unregistered_kind_is_rejected_naming_it_before_any_read() -> None:
    graph = _CountingGraph()
    unregistered = ReviewableKind(UNREGISTERED_KIND, "name", "global", "merge-above")

    with pytest.raises(UnknownReviewKindError, match=UNREGISTERED_KIND):
        drop_already_reviewed(graph, (_pair("a", "x"),), kind=unregistered)  # pyright: ignore[reportArgumentType]

    assert graph.calls == []
