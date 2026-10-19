"""A registered kind's review end to end: dedup, persist, drop-reviewed, list and the export gate.

The kind is the test-only `ProbeItem`. Nothing here drives `merge_baseline_graph` or restore with
it (no Role is wired to it); the points under test are the ones a new kind reaches through its spec.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from export._gateway_fakes import ExportRig

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from company_merge._per_instrument_rig import INSTRUMENT_A, seed_defined_node
from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.models import BaselineNode, DedupResult, NearMissPair
from ps_service.company_merge.pending_review import (
    drop_already_reviewed,
    list_pending_reviews,
    persist_pending_reviews,
)
from ps_service.company_merge.reviewable_kinds import ReviewThresholds
from ps_service.export.near_miss_gate import unresolved_near_miss_review_ids

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig
    from ps_service.company_merge.reviewable_kinds import ReviewableKind


def _dedup(rig: MergeRig, kind: ReviewableKind) -> DedupResult:
    nodes = (
        BaselineNode(id="p_1", properties={"name": "one"}, embedding=(1.0, 0.0)),
        BaselineNode(id="p_2", properties={"name": "two"}, embedding=(0.8, 0.6)),
    )
    return dedupe_canonical_nodes(
        nodes,
        kind=kind,
        single_tenant_graph=rig.legacy,
        model="fake-embed-model",
        thresholds=ReviewThresholds(review=0.7),
        instrument_id=INSTRUMENT_A,
    )


def _dedup_drop_persist(rig: MergeRig, kind: ReviewableKind) -> tuple[NearMissPair, ...]:
    """One ingest pass over the same input; returns the pairs it persisted."""
    result = _dedup(rig, kind)
    fresh = drop_already_reviewed(rig.legacy, result.near_misses, kind=kind)
    persist_pending_reviews(
        GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id),
        fresh,
        kind=kind,
        values=stepped_values(),
    )
    return fresh


def test_a_registered_kind_review_is_deduped_persisted_listed_and_blocks_export(
    merge_rig: MergeRig, test_only_reviewable_kind: ReviewableKind
) -> None:
    kind = test_only_reviewable_kind

    first = _dedup_drop_persist(merge_rig, kind)
    second = _dedup_drop_persist(merge_rig, kind)  # same input again, before the nodes are merged

    assert [(m.incoming_id, m.nearest_existing_id) for m in first] == [("p_2", "p_1")]
    assert second == ()
    for node_id in ("p_1", "p_2"):  # what the instrument's merge writes for the two nodes
        seed_defined_node(
            merge_rig, label=kind.label, node_id=node_id, name=node_id, instrument_id=INSTRUMENT_A
        )
    reviews = list_pending_reviews(merge_rig.legacy)
    assert [(r.kind, r.incoming_id, r.nearest_existing_id) for r in reviews] == [
        (kind.label, "p_2", "p_1")
    ]

    export_rig = ExportRig(merge_rig)
    for node_id in ("p_1", "p_2"):
        ExportRig.seed_node(export_rig.baseline, kind.label, node_id, {"name": node_id})
    blocking = unresolved_near_miss_review_ids(export_rig.baseline_reader(), merge_rig.legacy)

    assert blocking == (reviews[0].id,)
    # A later pass finds the pair already reviewed and the pool now holding both nodes.
    assert _dedup_drop_persist(merge_rig, kind) == ()
    assert len(list_pending_reviews(merge_rig.legacy)) == 1
