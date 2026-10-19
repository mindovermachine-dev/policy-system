"""Per-instrument scope and the `review-only` rule, offline path.

The offline twin of `test_per_instrument_review_kind.py`: the same pool and the same decision, with
the incoming embeddings artifact-supplied and no embedding fetch of any kind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from company_merge._per_instrument_rig import INSTRUMENT_A, INSTRUMENT_B, seed_defined_node
from ps_service.company_merge.dedup import resolve_capability_convergence_offline
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import BaselineNode, DedupResult
from ps_service.company_merge.pending_review import list_pending_reviews, persist_pending_reviews
from ps_service.company_merge.reviewable_kinds import ReviewThresholds

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import MergeRig
    from ps_service.company_merge.reviewable_kinds import ReviewableKind
    from ps_service.logging.emitter import LogEmitter

_REVIEW = ReviewThresholds(review=0.7)
_NEAR = (0.8, 0.6)


def _nodes(*ids: str) -> tuple[BaselineNode, ...]:
    return tuple(BaselineNode(id=i, properties={"name": f"name {i}"}) for i in ids)


def _offline(
    rig: MergeRig,
    kind: ReviewableKind,
    ids: tuple[str, ...],
    embeddings: dict[str, tuple[float, ...]],
    *,
    instrument_id: str = INSTRUMENT_A,
    emitter: LogEmitter | None = None,
) -> DedupResult:
    return resolve_capability_convergence_offline(
        _nodes(*ids),
        incoming_embeddings=embeddings,
        single_tenant_graph=rig.legacy,
        kind=kind,
        thresholds=_REVIEW,
        instrument_id=instrument_id,
        emitter=emitter,
    )


def test_two_nodes_defined_by_one_instrument_raise_one_review(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind

    result = _offline(merge_rig, kind, ("p_1", "p_2"), {"p_1": (1.0, 0.0), "p_2": _NEAR})
    persist_pending_reviews(
        GraphWriteTarget(merge_rig.gateway, POLICY_SYSTEM, merge_rig.audit_event_id),
        result.near_misses,
        kind=kind,
        values=stepped_values(),
    )
    for node_id in ("p_1", "p_2"):
        seed_defined_node(
            merge_rig, label=kind.label, node_id=node_id, name=node_id, instrument_id=INSTRUMENT_A
        )

    reviews = list_pending_reviews(merge_rig.legacy)
    assert [(r.kind, r.incoming_id, r.nearest_existing_id) for r in reviews] == [
        (kind.label, "p_2", "p_1")
    ]


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

    result = _offline(merge_rig, kind, ("p_new",), {"p_new": _NEAR})

    assert [(r.match_kind, r.canonical_id) for r in result.resolutions] == [("new", "p_new")]
    assert [(m.incoming_id, m.nearest_existing_id) for m in result.near_misses] == [
        ("p_new", "p_old")
    ]


def test_nodes_defined_by_different_instruments_raise_no_review(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind
    seed_defined_node(
        merge_rig,
        label=kind.label,
        node_id="p_other",
        name="other",
        instrument_id=INSTRUMENT_B,
        embedding=(1.0, 0.0),
    )

    result = _offline(merge_rig, kind, ("p_mine",), {"p_mine": (1.0, 0.0)})

    assert result.near_misses == ()
    assert [r.match_kind for r in result.resolutions] == ["new"]


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

    result = _offline(
        merge_rig, kind, ("p_twin", "p_close"), {"p_twin": _NEAR, "p_close": (0.6, 0.8)}
    )

    assert {r.match_kind for r in result.resolutions} <= {"exact", "new"}
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

    result = _offline(merge_rig, kind, ("p_far",), {"p_far": (0.0, 1.0)})

    assert result.near_misses == ()


def test_candidate_without_an_embedding_is_skipped_and_counted(
    merge_rig: MergeRig,
    test_only_reviewable_kind: ReviewableKind,
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
) -> None:
    kind = test_only_reviewable_kind
    emitter, log_path = make_emitter()
    seed_defined_node(
        merge_rig, label=kind.label, node_id="p_bare", name="bare", instrument_id=INSTRUMENT_A
    )

    result = _offline(merge_rig, kind, ("p_new",), {"p_new": (1.0, 0.0)}, emitter=emitter)

    assert result.near_misses == ()
    assert [r.match_kind for r in result.resolutions] == ["new"]
    emitter.flush()
    warnings = [e for e in read_lines(log_path) if e.get("outcome") == "warning"]
    assert [w["skipped_missing_embedding_count"] for w in warnings] == [1]
