"""A restore raises a Capability near miss only for a pair scoring at or above the review floor.

The real `restore_instrument` runs through the gateway rig over three incoming Capabilities
scoring 0.4, exactly 0.5 and 0.6 against Capabilities already held (`_review_floor_fixtures`),
at a merge threshold of 0.9. What the operator sees is read back the way `list-near-misses` and
the export gate read it: `list_pending_reviews` and `unresolved_near_miss_review_ids`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from company_merge._gateway_fakes import RigReadHandle
from company_merge._review_floor_fixtures import (
    ABOVE_ID,
    AT_ID,
    BELOW_ID,
    FLOOR,
    INCOMING,
    MERGE,
    restore_artifact,
    seed_existing,
)
from export._gateway_fakes import ExportRig
from graph_gateway._fakes import GatewayRig

from ps_service.company_merge.pending_review import list_pending_reviews
from ps_service.export.near_miss_gate import unresolved_near_miss_review_ids
from restore._near_miss_fixtures import POLICY_SYSTEM, reviews

if TYPE_CHECKING:
    from restore._gateway_fakes import RestoreRig


def _restore(rig: RestoreRig, *, review_threshold: float) -> int:
    seed_existing(rig.policy_system)
    outcome = rig.restore(restore_artifact(), threshold=MERGE, review_threshold=review_threshold)
    return outcome.near_miss_reviews_created


def test_AC_BI_005_AC_BI_006_only_pairs_at_or_above_the_floor_become_reviews(
    restore_rig: RestoreRig,
) -> None:
    created = _restore(restore_rig, review_threshold=FLOOR)

    found = reviews(restore_rig)
    assert {r["incoming_id"] for r in found.values()} == {AT_ID, ABOVE_ID}
    assert created == len(found) == 2
    by_incoming = {r["incoming_id"]: r["similarity"] for r in found.values()}
    assert by_incoming[AT_ID] == FLOOR  # exactly on the floor: kept (>=)
    assert by_incoming[ABOVE_ID] == pytest.approx(INCOMING[ABOVE_ID][3])
    # the below-floor Capability is still minted under its own id, just not reviewed
    assert ("Capability", BELOW_ID) in restore_rig.graph(POLICY_SYSTEM).nodes


def test_AC_BI_005_the_listing_an_operator_reads_omits_the_below_floor_pair(
    restore_rig: RestoreRig,
) -> None:
    _restore(restore_rig, review_threshold=FLOOR)

    listed = list_pending_reviews(RigReadHandle(restore_rig.graph(POLICY_SYSTEM)))

    assert {r.incoming_id for r in listed} == {AT_ID, ABOVE_ID}
    assert BELOW_ID not in {r.incoming_id for r in listed}


def test_AC_BI_005_only_the_at_or_above_floor_reviews_block_an_export(
    restore_rig: RestoreRig,
) -> None:
    _restore(restore_rig, review_threshold=FLOOR)
    export_rig = ExportRig(GatewayRig())
    for cap_id in INCOMING:
        ExportRig.seed_node(export_rig.baseline, "Capability", cap_id, {"text": "x"})
    live = RigReadHandle(restore_rig.graph(POLICY_SYSTEM))
    expected = {
        review_id
        for review_id, props in reviews(restore_rig).items()
        if props["incoming_id"] in {AT_ID, ABOVE_ID}
    }

    blocking = unresolved_near_miss_review_ids(export_rig.baseline_reader(), live)

    assert set(blocking) == expected
    assert len(blocking) == 2


def test_AC_BI_010_a_zero_floor_raises_every_review_as_before(restore_rig: RestoreRig) -> None:
    created = _restore(restore_rig, review_threshold=0.0)

    assert {r["incoming_id"] for r in reviews(restore_rig).values()} == set(INCOMING)
    assert created == len(INCOMING)


def test_AC_BI_009_a_floor_equal_to_the_merge_threshold_raises_no_review_below_it(
    restore_rig: RestoreRig,
) -> None:
    created = _restore(restore_rig, review_threshold=MERGE)

    assert reviews(restore_rig) == {}
    assert created == 0
