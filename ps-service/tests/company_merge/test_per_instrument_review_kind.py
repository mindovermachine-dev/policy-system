"""Per-instrument scope and the `review-only` rule, live path.

The kind is the test-only `ProbeItem` (per-instrument, review-only). Its candidates are the nodes
the run's instrument `DEFINES` in `policy_system` plus the run's own mints; a close pair is
reviewed and never merged. The rig is the in-memory gateway, so a persisted review is read back
through `list_pending_reviews` exactly as the API does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from company_merge._per_instrument_rig import INSTRUMENT_A, INSTRUMENT_B, seed_defined_node
from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import BaselineNode, DedupResult
from ps_service.company_merge.pending_review import list_pending_reviews, persist_pending_reviews
from ps_service.company_merge.reviewable_kinds import ReviewableKind, ReviewThresholds

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig

_MODEL = "fake-embed-model"
_REVIEW = ReviewThresholds(review=0.7)
_NEAR = (0.8, 0.6)  # cosine 0.8 against (1, 0)
_FAR = (0.0, 1.0)  # cosine 0.0 against (1, 0)


class _RecordingCaller:
    """An `EmbeddingCaller` that must not be needed: it records each call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del timeout
        self.calls.append(inputs[0])
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=[1.0, 0.0], index=0, object="embedding")]
        )


def _incoming(node_id: str, vector: tuple[float, ...]) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": f"name {node_id}"}, embedding=vector)


def _dedup(
    rig: MergeRig,
    kind: ReviewableKind,
    nodes: tuple[BaselineNode, ...],
    caller: _RecordingCaller,
    *,
    instrument_id: str = INSTRUMENT_A,
    thresholds: ReviewThresholds = _REVIEW,
) -> DedupResult:
    return dedupe_canonical_nodes(
        nodes,
        kind=kind,
        single_tenant_graph=rig.legacy,
        model=_MODEL,
        thresholds=thresholds,
        instrument_id=instrument_id,
        call_embedding=caller,
    )


def _persist(rig: MergeRig, kind: ReviewableKind, result: DedupResult) -> None:
    persist_pending_reviews(
        GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id),
        result.near_misses,
        kind=kind,
        values=stepped_values(),
    )


def test_two_nodes_defined_by_one_instrument_raise_one_review(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind
    caller = _RecordingCaller()

    result = _dedup(
        merge_rig, kind, (_incoming("p_1", (1.0, 0.0)), _incoming("p_2", _NEAR)), caller
    )
    _persist(merge_rig, kind, result)
    for node_id in ("p_1", "p_2"):  # the nodes the instrument's merge writes
        seed_defined_node(
            merge_rig, label=kind.label, node_id=node_id, name=node_id, instrument_id=INSTRUMENT_A
        )

    reviews = list_pending_reviews(merge_rig.legacy)
    assert [(r.kind, r.incoming_id, r.nearest_existing_id) for r in reviews] == [
        (kind.label, "p_2", "p_1")
    ]
    assert caller.calls == []


def test_new_node_pairs_with_a_node_the_instrument_already_defines(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind
    seed_defined_node(
        merge_rig,
        label=kind.label,
        node_id="p_old",
        name="old",
        instrument_id=INSTRUMENT_A,
        embedding=(1.0, 0.0),
    )
    caller = _RecordingCaller()

    result = _dedup(merge_rig, kind, (_incoming("p_new", _NEAR),), caller)
    _persist(merge_rig, kind, result)
    seed_defined_node(
        merge_rig, label=kind.label, node_id="p_new", name="new", instrument_id=INSTRUMENT_A
    )

    assert [(r.match_kind, r.canonical_id) for r in result.resolutions] == [("new", "p_new")]
    reviews = list_pending_reviews(merge_rig.legacy)
    assert [(r.kind, r.incoming_id, r.nearest_existing_id) for r in reviews] == [
        (kind.label, "p_new", "p_old")
    ]


def test_nodes_defined_by_different_instruments_raise_no_review(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind
    seed_defined_node(  # no cached embedding: scoring it would need a call
        merge_rig, label=kind.label, node_id="p_other", name="other", instrument_id=INSTRUMENT_B
    )
    seed_defined_node(  # defined by nobody: not in any instrument's pool
        merge_rig, label=kind.label, node_id="p_orphan", name="orphan", instrument_id=None
    )
    caller = _RecordingCaller()

    result = _dedup(merge_rig, kind, (_incoming("p_mine", (1.0, 0.0)),), caller)

    assert result.near_misses == ()
    assert [r.match_kind for r in result.resolutions] == ["new"]
    assert caller.calls == []


def test_an_exact_hit_on_a_node_the_instrument_defines_raises_no_review(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind
    seed_defined_node(
        merge_rig,
        label=kind.label,
        node_id="p_same",
        name="same",
        instrument_id=INSTRUMENT_A,
        embedding=(1.0, 0.0),
    )

    result = _dedup(merge_rig, kind, (_incoming("p_same", (1.0, 0.0)),), _RecordingCaller())

    assert [(r.match_kind, r.canonical_id) for r in result.resolutions] == [("exact", "p_same")]
    assert result.near_misses == ()


def test_review_only_kind_never_merges_a_pair_above_the_review_threshold(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind
    seed_defined_node(
        merge_rig,
        label=kind.label,
        node_id="p_old",
        name="old",
        instrument_id=INSTRUMENT_A,
        embedding=(1.0, 0.0),
    )
    twin = _incoming("p_twin", _NEAR)

    result = _dedup(merge_rig, kind, (twin, _incoming("p_close", (0.6, 0.8))), _RecordingCaller())

    assert {r.match_kind for r in result.resolutions} <= {"exact", "new"}
    assert [r.canonical_id for r in result.resolutions] == ["p_twin", "p_close"]
    assert [(m.incoming_id, m.nearest_existing_id) for m in result.near_misses] == [
        ("p_twin", "p_old"),
        ("p_close", "p_twin"),
    ]


def test_pair_below_the_review_threshold_raises_no_review(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind
    seed_defined_node(
        merge_rig,
        label=kind.label,
        node_id="p_old",
        name="old",
        instrument_id=INSTRUMENT_A,
        embedding=(1.0, 0.0),
    )

    result = _dedup(merge_rig, kind, (_incoming("p_far", _FAR),), _RecordingCaller())

    assert result.near_misses == ()
    assert [r.match_kind for r in result.resolutions] == ["new"]


@pytest.mark.parametrize(("review", "expected"), [(0.8, 1), (0.81, 0)])
def test_the_review_threshold_is_inclusive(
    merge_rig: MergeRig,
    test_only_reviewable_kind: ReviewableKind,
    review: float,
    expected: int,
) -> None:
    kind = test_only_reviewable_kind
    seed_defined_node(
        merge_rig,
        label=kind.label,
        node_id="p_old",
        name="old",
        instrument_id=INSTRUMENT_A,
        embedding=(1.0, 0.0),
    )

    result = _dedup(
        merge_rig,
        kind,
        (_incoming("p_new", _NEAR),),
        _RecordingCaller(),
        thresholds=ReviewThresholds(review=review),
    )

    assert len(result.near_misses) == expected
