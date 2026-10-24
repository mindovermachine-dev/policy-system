"""A restore guards `policy_system` with its log position, retrying up to three times.

The `policy_system` leg is built from reads taken at a log position P and
submitted with `ExpectedPosition(P)`; if any group landed since, the gateway refuses the WHOLE set
(nothing logged on any graph) and the restore reads again. Three refused attempts raise
the existing `RestoreConcurrencyConflictError` (same message); only the concurrent writes remain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.restore.errors import RestoreConcurrencyConflictError
from ps_service.restore.graph_submission import MAX_POLICY_SYSTEM_ATTEMPTS
from restore._fixtures import build_restore_artifact, small_graphs
from restore._gateway_fakes import install_racing_gateway
from restore._near_miss_fixtures import INCOMING, near_miss_artifact, reviews, seed_existing

if TYPE_CHECKING:
    from restore._gateway_fakes import RestoreRig

_POLICY_SYSTEM = "policy_system"


def _artifact():
    native, baseline = small_graphs()
    return build_restore_artifact(
        instrument_id="GW214-1.0", short_name="GW214", native_graph=native, baseline_graph=baseline
    )


def test_an_undisturbed_restore_succeeds_on_the_first_attempt(restore_rig: RestoreRig) -> None:
    racing = install_racing_gateway(restore_rig, races=0)

    restore_rig.restore(_artifact())

    assert racing.submits == 1


def test_a_write_between_the_read_and_the_submit_forces_a_recompute_and_both_writes_survive(
    restore_rig: RestoreRig,
) -> None:
    racing = install_racing_gateway(restore_rig, races=1)

    restore_rig.restore(_artifact())

    assert racing.submits == 2  # the first set was refused, the second took effect
    policy_system = restore_rig.graph(_POLICY_SYSTEM)
    assert ("Capability", "rival-1") in policy_system.nodes
    assert ("Capability", "cap_1") in policy_system.nodes
    assert ("ARTICLE", "art_1") in restore_rig.graph("gw214_native").nodes


def test_a_refused_attempt_logs_nothing_on_any_graph(restore_rig: RestoreRig) -> None:
    install_racing_gateway(restore_rig, races=1)

    restore_rig.restore(_artifact())

    # the rival group, then the restore's three legs: the refused first attempt left no trace
    assert [g.graph for g in restore_rig.store.groups].count("gw214_native") == 1
    assert restore_rig.store.multi_appends == [("gw214_baseline", "gw214_native", _POLICY_SYSTEM)]


def test_three_refused_attempts_raise_the_concurrency_conflict_and_only_the_rival_writes_remain(
    restore_rig: RestoreRig,
) -> None:
    racing = install_racing_gateway(restore_rig, races=MAX_POLICY_SYSTEM_ATTEMPTS)

    with pytest.raises(RestoreConcurrencyConflictError) as raised:
        restore_rig.restore(_artifact())

    assert racing.submits == MAX_POLICY_SYSTEM_ATTEMPTS
    assert str(raised.value) == (
        "restore merge into 'policy_system' aborted after 3 attempts due to a concurrent writer; "
        "no changes were made to the live graph"
    )
    assert restore_rig.store.multi_appends == []
    assert {g.graph for g in restore_rig.store.groups} == {_POLICY_SYSTEM}
    assert set(restore_rig.graph(_POLICY_SYSTEM).nodes) == {
        ("Capability", f"rival-{n}") for n in (1, 2, 3)
    }
    assert (
        "gw214_native" not in restore_rig.graphs.graphs
        or not restore_rig.graph("gw214_native").nodes
    )


def test_AC_BI_007_a_retry_rebuilds_the_reviews_so_one_race_leaves_one_set(
    restore_rig: RestoreRig,
) -> None:
    seed_existing(restore_rig)
    racing = install_racing_gateway(restore_rig, races=1)

    outcome = restore_rig.restore(near_miss_artifact())

    assert racing.submits == 2
    assert outcome.near_miss_reviews_created == len(INCOMING)
    assert len(reviews(restore_rig)) == len(INCOMING)  # not twice that: the refused set is gone


def test_AC_BI_007_three_refused_attempts_leave_no_review(restore_rig: RestoreRig) -> None:
    seed_existing(restore_rig)
    install_racing_gateway(restore_rig, races=MAX_POLICY_SYSTEM_ATTEMPTS)

    with pytest.raises(RestoreConcurrencyConflictError):
        restore_rig.restore(near_miss_artifact())

    assert reviews(restore_rig) == {}
