"""Ingest raises a Capability near miss only for a pair scoring at or above the review floor.

The real `merge_baseline_graph` runs over a gateway rig with three incoming Capabilities scoring
0.4, exactly 0.5 and 0.6 against Capabilities already held (`_review_floor_fixtures`), merge
threshold 0.9. What persists is read back as `list-near-misses` (`list_pending_reviews`) and the
export gate (`unresolved_near_miss_review_ids`) read it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from export._gateway_fakes import ExportRig
from graph_gateway._fakes import GatewayRig

from company_merge._gateway_fakes import FamilyABaseline, RigReadHandle, family_a_baseline_graph
from company_merge._review_floor_fixtures import (
    ABOVE_ID,
    AT_ID,
    BELOW_ID,
    EMBED_MODEL,
    EXISTING,
    FLOOR,
    INCOMING,
    INSTRUMENT_ID,
    MERGE,
    ByNameEmbedding,
    Vector,
    ingest_baseline,
    seed_existing,
)
from company_merge._seed_merge_graphs import BaselineGraphReader
from company_merge._thresholds import ROLE_REVIEW_THRESHOLD
from ps_service.company_merge.merge import MergeResult, merge_baseline_graph
from ps_service.company_merge.pending_review import list_pending_reviews
from ps_service.export.near_miss_gate import unresolved_near_miss_review_ids

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from company_merge._gateway_fakes import MergeRig


def _merge(
    rig: MergeRig,
    emitter: object,
    baseline: FamilyABaseline,
    *,
    review: float,
    merge: float = MERGE,
    extra_vectors: dict[str, Vector] | None = None,
) -> MergeResult:
    return merge_baseline_graph(
        baseline.instrument_id,
        baseline_graph=BaselineGraphReader(family_a_baseline_graph(baseline)),
        single_tenant_graph=rig.legacy,
        embed_model=EMBED_MODEL,
        merge_threshold=merge,
        review_threshold=review,
        role_review_threshold=ROLE_REVIEW_THRESHOLD,
        call_embedding=ByNameEmbedding(extra_vectors),
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        **rig.merge_keywords(),
    )


def _review_nodes(rig: MergeRig) -> dict[str, dict[str, object]]:
    return {
        node_id: props
        for (label, node_id), props in rig.policy_system.nodes.items()
        if label == "PendingReview"
    }


def test_AC_BI_005_a_pair_below_the_floor_raises_no_review_and_no_near_miss_log(
    merge_rig: MergeRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    seed_existing(merge_rig.policy_system)

    result = _merge(merge_rig, emitter, ingest_baseline(BELOW_ID), review=FLOOR)

    assert result.near_misses == ()
    assert result.pending_review_count == 0
    assert _review_nodes(merge_rig) == {}
    assert not [e for e in merge_rig.logged() if e.name == "PendingReview"]
    emitter.flush()
    assert [e for e in read_lines(log_path) if e.get("outcome") == "near_miss"] == []
    # it is minted under its own id, as any non-matching Capability is
    assert ("Capability", BELOW_ID) in merge_rig.policy_system.nodes


def test_AC_BI_006_a_pair_above_the_floor_raises_one_review_with_its_score(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    seed_existing(merge_rig.policy_system)

    result = _merge(merge_rig, emitter, ingest_baseline(ABOVE_ID), review=FLOOR)

    (review,) = _review_nodes(merge_rig).values()
    assert (review["incoming_id"], review["nearest_existing_id"]) == (ABOVE_ID, "cap_ex_above")
    assert review["similarity"] == pytest.approx(INCOMING[ABOVE_ID][3])
    assert [n.incoming_id for n in result.near_misses] == [ABOVE_ID]
    assert ("Capability", ABOVE_ID) in merge_rig.policy_system.nodes  # minted new, not merged


def test_AC_BI_006_a_pair_scoring_exactly_the_floor_is_reviewed(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    seed_existing(merge_rig.policy_system)

    _merge(merge_rig, emitter, ingest_baseline(AT_ID), review=FLOOR)

    (review,) = _review_nodes(merge_rig).values()
    assert review["incoming_id"] == AT_ID
    assert review["similarity"] == FLOOR


def test_AC_BI_005_AC_BI_006_the_listing_and_the_export_gate_see_only_floor_clearing_pairs(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    seed_existing(merge_rig.policy_system)
    _merge(merge_rig, emitter, ingest_baseline(), review=FLOOR)
    live = RigReadHandle(merge_rig.policy_system)
    export_rig = ExportRig(GatewayRig())
    for cap_id in INCOMING:
        ExportRig.seed_node(export_rig.baseline, "Capability", cap_id, {"text": "x"})

    listed = list_pending_reviews(live)
    blocking = unresolved_near_miss_review_ids(export_rig.baseline_reader(), live)

    assert {r.incoming_id for r in listed} == {AT_ID, ABOVE_ID}
    assert {r.id for r in listed if r.incoming_id in {AT_ID, ABOVE_ID}} == set(blocking)
    assert len(blocking) == 2
    assert BELOW_ID not in {r.incoming_id for r in listed}


def test_AC_BI_007_an_eligible_match_at_the_merge_threshold_resolves_as_before(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    seed_existing(merge_rig.policy_system)
    twin = INCOMING[AT_ID][1]  # the AT Capability, here given its neighbour's exact vector
    result = _merge(
        merge_rig,
        emitter,
        ingest_baseline(AT_ID),
        review=FLOOR,
        extra_vectors={twin: EXISTING["cap_ex_at"][0]},
    )

    assert result.near_misses == ()
    assert _review_nodes(merge_rig) == {}
    assert ("Capability", AT_ID) not in merge_rig.policy_system.nodes  # merged onto cap_ex_at


@pytest.mark.parametrize(("second_score", "expected_reviews"), [(0.7, 1), (0.4, 0)])
def test_AC_BI_008_a_same_run_mint_is_a_review_target_only_at_or_above_the_floor(
    merge_rig: MergeRig, make_emitter: MakeEmitter, second_score: float, expected_reviews: int
) -> None:
    emitter, _ = make_emitter()
    baseline = FamilyABaseline(
        instrument_id=INSTRUMENT_ID,
        instrument_properties={"id": INSTRUMENT_ID, "title": "NM349"},
        capabilities={"cap_first": ("First Capability", 0.8), "cap_second": ("Second One", 0.8)},
    )
    first: Vector = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    rest = (1.0 - second_score * second_score) ** 0.5
    second: Vector = (second_score, rest, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    result = _merge(
        merge_rig,
        emitter,
        baseline,
        review=FLOOR,
        extra_vectors={"First Capability": first, "Second One": second},
    )

    reviews = list(_review_nodes(merge_rig).values())
    assert len(reviews) == expected_reviews == len(result.near_misses)
    assert all(r["nearest_existing_id"] == "cap_first" for r in reviews)
    assert ("Capability", "cap_second") in merge_rig.policy_system.nodes  # never merged


def test_AC_BI_009_a_floor_equal_to_the_merge_threshold_raises_no_review_below_it(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    seed_existing(merge_rig.policy_system)

    result = _merge(merge_rig, emitter, ingest_baseline(), review=MERGE)

    assert result.near_misses == ()
    assert _review_nodes(merge_rig) == {}


def test_AC_BI_010_a_zero_floor_reviews_every_pair_as_before(
    merge_rig: MergeRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    seed_existing(merge_rig.policy_system)

    _merge(merge_rig, emitter, ingest_baseline(), review=0.0)

    assert {r["incoming_id"] for r in _review_nodes(merge_rig).values()} == set(INCOMING)
