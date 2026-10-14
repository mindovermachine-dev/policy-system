"""A restore is applied by swap: readers see none or all of it.

Covered: a reader sees none or all of the instrument; a restore that dies after the log commit is
completed by recovery and no reader sees a partial instrument before or after; restores and other
writers on overlapping graphs serialize without a deadlock. All on the Protocol fakes. The gateway
rig's in-memory key space records every copy, staged write and rename and lets a test look at the
three live graphs at each of those points, as a reader could.
A `Crash` (a `BaseException`) raised from a hook stops the restore the way a killed process
would; `restart()` builds a fresh gateway over the same log and key space.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from graph_gateway._fakes import Fault
from graph_gateway._swap_support import WAIT_SECONDS, Crash, Thread

from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.restore.errors import RestoreApplyPendingError, RestoreGraphWriteFailedError
from restore._fixtures import build_restore_artifact, small_graphs

if TYPE_CHECKING:
    from graph_gateway._fakes import Keys

    from ps_service.restore.models import RestoreArtifact
    from restore._gateway_fakes import RestoreRig

_POLICY_SYSTEM = "policy_system"
_WRITER_EVENT_ID = "6f1c2b3a-0d4e-4f5a-8b6c-7d8e9f0a1b2c"
_CRASH_POINTS = ("before_copy", "staged_write", "before_swap", "after_swap", "first_marker")


def _artifact(short: str) -> RestoreArtifact:
    instrument_id = f"{short}-1.0"
    native, baseline = small_graphs(instrument_id=instrument_id)
    return build_restore_artifact(
        instrument_id=instrument_id,
        short_name=short,
        native_graph=native,
        baseline_graph=baseline,
    )


def _graphs(short: str) -> tuple[str, str, str]:
    stem = short.lower()
    return (f"{stem}_baseline", f"{stem}_native", _POLICY_SYSTEM)


def _state(rig: RestoreRig, short: str) -> tuple[Keys, ...]:
    return rig.key_port.observe(*_graphs(short))


def _crash_at(rig: RestoreRig, point: str) -> None:
    def die(*_: object) -> None:
        raise Crash

    if point == "before_copy":
        rig.key_port.before_copy = die
    elif point == "staged_write":
        rig.key_port.on_staged_write = die
    elif point == "before_swap":
        rig.key_port.before_swap = die
    elif point == "after_swap":
        rig.key_port.after_swap = die
    else:
        rig.store.on_advance = die


def _clear_hooks(rig: RestoreRig) -> None:
    rig.key_port.before_copy = None
    rig.key_port.on_staged_write = None
    rig.key_port.before_swap = None
    rig.key_port.after_swap = None
    rig.store.on_advance = None


def _record_readers(rig: RestoreRig, short: str) -> list[tuple[Keys, ...]]:
    """Record what a reader would see at every staged write, before the flip and after it."""
    seen: list[tuple[Keys, ...]] = []

    def record(*_: object) -> None:
        seen.append(_state(rig, short))

    rig.key_port.on_staged_write = record
    rig.key_port.before_swap = record
    rig.key_port.after_swap = record
    return seen


def _seeded(rig: RestoreRig) -> tuple[Keys, ...]:
    """Restore a first instrument so `policy_system` is not empty; return the target's state."""
    rig.restore(_artifact("OLD214"))
    return _state(rig, "NEW214")


def _replayed_digests_equal_live(rig: RestoreRig, short: str) -> bool:
    live = tuple(canonical_digest(rig.graph(graph)) for graph in _graphs(short))
    for graph in _graphs(short):
        # A restore now records a digest checkpoint, and a replay verifies against it, so the log
        # is replayed into an empty graph (as after FalkorDB lost it), not over the live content.
        rig.graph(graph).flush()
        rig.gateway.replay_graph(graph)
    return live == tuple(canonical_digest(rig.graph(graph)) for graph in _graphs(short))


# -- none or all ------------------------------------------------------------------


def test_a_restore_is_applied_by_one_swap_of_all_three_graphs(restore_rig: RestoreRig) -> None:
    restore_rig.restore(_artifact("NEW214"))

    assert [sorted(live for _, live in swap) for swap in restore_rig.key_port.swaps] == [
        sorted(_graphs("NEW214"))
    ]
    assert len(restore_rig.key_port.copies) == 3
    assert restore_rig.key_port.staged_names() == []


def test_a_reader_sees_none_or_all_of_the_instrument_at_every_step(
    restore_rig: RestoreRig,
) -> None:
    before = _seeded(restore_rig)
    seen = _record_readers(restore_rig, "NEW214")

    restore_rig.restore(_artifact("NEW214"))
    after = _state(restore_rig, "NEW214")

    assert before != after
    assert len(seen) >= 3  # staged writes, the instant before the flip, and the one after it
    assert all(state in {before, after} for state in seen)
    assert seen[-1] == after
    assert all(state == before for state in seen[:-1])  # nothing visible until the flip


def test_a_restore_over_an_existing_copy_is_also_none_or_all(restore_rig: RestoreRig) -> None:
    restore_rig.restore(_artifact("NEW214"))
    before = _state(restore_rig, "NEW214")
    native, baseline = small_graphs(instrument_id="NEW214-1.0", capability_name="Renamed")
    changed = build_restore_artifact(
        instrument_id="NEW214-1.0",
        short_name="NEW214",
        native_graph=native,
        baseline_graph=baseline,
    )
    seen = _record_readers(restore_rig, "NEW214")

    restore_rig.restore(changed)

    assert all(state == before for state in seen)  # same node keys on both sides of the flip
    assert restore_rig.graph("new214_baseline").nodes[("Capability", "cap_1")]["name"] == "Renamed"


# -- a restore that dies after the commit is completed ------------------------------


@pytest.mark.parametrize("point", _CRASH_POINTS)
def test_recovery_completes_a_restore_that_died_at_any_step_and_readers_see_none_or_all(
    restore_rig: RestoreRig, point: str
) -> None:
    before = _seeded(restore_rig)
    _crash_at(restore_rig, point)
    with pytest.raises(Crash):
        restore_rig.restore(_artifact("NEW214"))
    mid = _state(restore_rig, "NEW214")
    _clear_hooks(restore_rig)
    restore_rig.restart()
    seen = _record_readers(restore_rig, "NEW214")

    result = restore_rig.gateway.recover()
    after = _state(restore_rig, "NEW214")

    assert result.gated == ()
    assert before != after
    assert mid in {before, after}  # what the dead process left is no mix
    assert all(state in {before, after} for state in seen)  # nor is anything recovery shows
    assert restore_rig.key_port.staged_names() == []
    assert all(restore_rig.gateway.is_caught_up(graph) for graph in _graphs("NEW214"))
    assert _replayed_digests_equal_live(restore_rig, "NEW214")


def test_a_restore_whose_copy_fails_is_committed_but_invisible_until_it_is_completed(
    restore_rig: RestoreRig,
) -> None:
    before = _seeded(restore_rig)
    fault = redis.exceptions.ConnectionError("Error 111 connecting to 10.1.2.3:6379.")
    restore_rig.key_port.copy_fault = Fault(fault)

    with pytest.raises(RestoreApplyPendingError):
        restore_rig.restore(_artifact("NEW214"))

    assert _state(restore_rig, "NEW214") == before  # committed, not visible, on any graph
    restore_rig.key_port.copy_fault = None
    restore_rig.gateway.catch_up("new214_native")
    assert _state(restore_rig, "NEW214") != before
    assert all(restore_rig.gateway.is_caught_up(graph) for graph in _graphs("NEW214"))


def test_a_swap_the_engine_keeps_refusing_leaves_the_restore_invisible_and_blocks_its_graphs(
    restore_rig: RestoreRig,
) -> None:
    before = _seeded(restore_rig)
    restore_rig.key_port.swap_conflicts = 100

    with pytest.raises(RestoreApplyPendingError):
        restore_rig.restore(_artifact("NEW214"))
    assert _state(restore_rig, "NEW214") == before
    with pytest.raises(RestoreGraphWriteFailedError):
        restore_rig.restore(_artifact("OTHER214"))  # every leg of the pending set refuses writers
    assert _state(restore_rig, "NEW214") == before

    restore_rig.key_port.swap_conflicts = 0
    restore_rig.gateway.catch_up("new214_native")

    assert _state(restore_rig, "NEW214") != before


# -- serialization ------------------------------------------------------------------


def test_two_restores_of_different_instruments_both_land_without_a_deadlock(
    restore_rig: RestoreRig,
) -> None:
    first = Thread(lambda: restore_rig.restore(_artifact("AAA214"))).start()
    second = Thread(lambda: restore_rig.restore(_artifact("BBB214"))).start()

    assert first.finishes_within(WAIT_SECONDS)
    assert second.finishes_within(WAIT_SECONDS)
    assert first.error is None
    assert second.error is None
    for short in ("AAA214", "BBB214"):
        assert ("RegulatoryInstrument", f"{short}-1.0") in restore_rig.graph(_POLICY_SYSTEM).nodes
        assert ("ARTICLE", "art_1") in restore_rig.graph(f"{short.lower()}_native").nodes
    appended = restore_rig.store.multi_appends
    assert len(appended) == 2
    assert all(order == tuple(sorted(order)) for order in appended)  # the documented lock order


def test_a_restore_and_another_writer_on_policy_system_serialize(restore_rig: RestoreRig) -> None:
    restore_rig.store.audit_rows.add(_WRITER_EVENT_ID)
    restore = Thread(lambda: restore_rig.restore(_artifact("NEW214"))).start()
    writers = [
        Thread(
            lambda i=i: restore_rig.gateway.submit_group(
                MutationGroup(
                    graph=_POLICY_SYSTEM,
                    audit_event_id=_WRITER_EVENT_ID,
                    primitives=(UpsertNode(label="Capability", id=f"rival-{i}"),),
                )
            )
        ).start()
        for i in range(3)
    ]

    assert restore.finishes_within(WAIT_SECONDS)
    assert all(writer.finishes_within(WAIT_SECONDS) for writer in writers)
    assert (
        restore.error is None or type(restore.error).__name__ == "RestoreConcurrencyConflictError"
    )
    assert all(writer.error is None for writer in writers)
    deadline = time.monotonic() + WAIT_SECONDS
    while not restore_rig.gateway.is_caught_up(_POLICY_SYSTEM) and time.monotonic() < deadline:
        time.sleep(0.005)
    for i in range(3):
        assert ("Capability", f"rival-{i}") in restore_rig.graph(_POLICY_SYSTEM).nodes
