"""A restore guards `policy_system` with its log position, retrying up to three times.

The `policy_system` leg is built from reads taken at a log position P and
submitted with `ExpectedPosition(P)`; if any group landed since, the gateway refuses the WHOLE set
(nothing logged on any graph) and the restore reads again. Three refused attempts raise
the existing `RestoreConcurrencyConflictError` (same message); only the concurrent writes remain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.restore.errors import RestoreConcurrencyConflictError
from ps_service.restore.graph_submission import MAX_POLICY_SYSTEM_ATTEMPTS
from restore._fixtures import build_restore_artifact, small_graphs
from restore._near_miss_fixtures import INCOMING, near_miss_artifact, reviews, seed_existing

if TYPE_CHECKING:
    from ps_service.graph_gateway.gateway import GraphWriteGateway
    from ps_service.graph_gateway.models import MultiGraphGroup, MultiGraphOutcome
    from restore._gateway_fakes import RestoreRig

_POLICY_SYSTEM = "policy_system"
_WRITER_EVENT_ID = "6f1c2b3a-0d4e-4f5a-8b6c-7d8e9f0a1b2c"


class _RacingGateway:
    """The rig's gateway, except another writer lands a group after each of the first N reads.

    The competing group is written between the restore's position read and its submit, which is
    exactly the window `ExpectedPosition` guards.
    """

    def __init__(self, rig: RestoreRig, *, races: int) -> None:
        self._rig = rig
        self._inner: GraphWriteGateway = rig.gateway
        self._races = races
        self.rivals = 0
        self.submits = 0

    def read_log_position(self, graph: str) -> int:
        position = self._inner.read_log_position(graph)
        if graph == _POLICY_SYSTEM and self.rivals < self._races:
            self.rivals += 1
            self._rig.store.audit_rows.add(_WRITER_EVENT_ID)
            self._inner.submit_group(
                MutationGroup(
                    graph=_POLICY_SYSTEM,
                    audit_event_id=_WRITER_EVENT_ID,
                    primitives=(
                        UpsertNode(
                            label="Capability",
                            id=f"rival-{self.rivals}",
                            properties={"name": f"rival {self.rivals}"},
                        ),
                    ),
                )
            )
        return position

    def submit_multi_graph_group(self, group: MultiGraphGroup) -> MultiGraphOutcome:
        self.submits += 1
        return self._inner.submit_multi_graph_group(group)

    def submit_group(self, group: MutationGroup) -> object:
        return self._inner.submit_group(group)

    def is_caught_up(self, graph: str) -> bool:
        return self._inner.is_caught_up(graph)

    def catch_up(self, graph: str) -> object:
        return self._inner.catch_up(graph)


def _artifact():
    native, baseline = small_graphs()
    return build_restore_artifact(
        instrument_id="GW214-1.0", short_name="GW214", native_graph=native, baseline_graph=baseline
    )


def _install(rig: RestoreRig, *, races: int) -> _RacingGateway:
    racing = _RacingGateway(rig, races=races)
    rig.open_gateway = lambda: racing  # type: ignore[method-assign]  # a rig stand-in
    return racing


def test_an_undisturbed_restore_succeeds_on_the_first_attempt(restore_rig: RestoreRig) -> None:
    racing = _install(restore_rig, races=0)

    restore_rig.restore(_artifact())

    assert racing.submits == 1


def test_a_write_between_the_read_and_the_submit_forces_a_recompute_and_both_writes_survive(
    restore_rig: RestoreRig,
) -> None:
    racing = _install(restore_rig, races=1)

    restore_rig.restore(_artifact())

    assert racing.submits == 2  # the first set was refused, the second took effect
    policy_system = restore_rig.graph(_POLICY_SYSTEM)
    assert ("Capability", "rival-1") in policy_system.nodes
    assert ("Capability", "cap_1") in policy_system.nodes
    assert ("ARTICLE", "art_1") in restore_rig.graph("gw214_native").nodes


def test_a_refused_attempt_logs_nothing_on_any_graph(restore_rig: RestoreRig) -> None:
    _install(restore_rig, races=1)

    restore_rig.restore(_artifact())

    # the rival group, then the restore's three legs: the refused first attempt left no trace
    assert [g.graph for g in restore_rig.store.groups].count("gw214_native") == 1
    assert restore_rig.store.multi_appends == [("gw214_baseline", "gw214_native", _POLICY_SYSTEM)]


def test_three_refused_attempts_raise_the_concurrency_conflict_and_only_the_rival_writes_remain(
    restore_rig: RestoreRig,
) -> None:
    racing = _install(restore_rig, races=MAX_POLICY_SYSTEM_ATTEMPTS)

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
    racing = _install(restore_rig, races=1)

    outcome = restore_rig.restore(near_miss_artifact())

    assert racing.submits == 2
    assert outcome.near_miss_reviews_created == len(INCOMING)
    assert len(reviews(restore_rig)) == len(INCOMING)  # not twice that: the refused set is gone


def test_AC_BI_007_three_refused_attempts_leave_no_review(restore_rig: RestoreRig) -> None:
    seed_existing(restore_rig)
    _install(restore_rig, races=MAX_POLICY_SYSTEM_ATTEMPTS)

    with pytest.raises(RestoreConcurrencyConflictError):
        restore_rig.restore(near_miss_artifact())

    assert reviews(restore_rig) == {}
