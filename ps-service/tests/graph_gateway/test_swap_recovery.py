"""An interrupted swap set is completed as one unit; readers see none or all.

Every test drives the gateway against the
Protocol fakes. A crash is a `Crash` (a `BaseException`) raised by a hook of the in-memory key port
or log store, so nothing in the gateway catches it and no cleanup runs, as when a process dies;
`GatewayRig.restart()` then builds a fresh gateway over the same log and key space. At every
observation point (during the staged build, at the flip, after it, after recovery) the three live
graphs hold the state before the set on all three or the state after it on all three, never a mix.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import Fault, GatewayRig, SteppedWait
from graph_gateway._swap_support import (
    BLOCKED_PROBE_SECONDS,
    OTHER_EVENT_ID,
    THREE,
    WAIT_SECONDS,
    Crash,
    Gate,
    Thread,
    digests,
    leg,
    live_nodes,
    node,
    staged_keys,
    swap_rig,
    swap_set,
    three_legs,
)
from ps_service.graph_gateway.entry_codec import encode_primitive
from ps_service.graph_gateway.errors import (
    GraphApplyBlockedError,
    GraphApplyError,
    GraphUnavailableError,
)
from ps_service.graph_gateway.models import GraphLogGroupDraft, MutationGroup, RecoveryResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_gateway._fakes import Keys

_DOWN = redis.exceptions.ConnectionError("Error 111 connecting to 10.1.2.3:6379.")
_OLD: Keys = frozenset({("Capability", "old")})
_OLD_THREE: tuple[Keys, Keys, Keys] = (_OLD, _OLD, _OLD)
_MERGED_THREE: tuple[Keys, Keys, Keys] = (
    _OLD | {("Capability", "b-1"), ("Capability", "b-2"), ("Capability", "b-3")},
    _OLD | {("Capability", "n-1")},
    _OLD | {("Capability", "p-1"), ("Capability", "p-2")},
)
_CRASH_POINTS = ("before_copy", "staged_write", "before_swap", "after_swap", "first_marker")


def _seeded(reconciler_wait: Callable[[float], bool] | None = None) -> GatewayRig:
    """A swap rig whose three graphs already hold an applied `old` node (log positions 1)."""
    rig = swap_rig(reconciler_wait=reconciler_wait)
    rig.gateway.submit_multi_graph_group(
        swap_set(
            leg("native", node("old")),
            leg("baseline", node("old")),
            leg("policy_system", node("old")),
            audit_event_id=OTHER_EVENT_ID,
            apply="in_place",
        )
    )
    return rig


def _state(rig: GatewayRig) -> tuple[Keys, ...]:
    return rig.key_port.observe(*THREE)


def _crash_at(rig: GatewayRig, point: str) -> None:
    """Make the process die at `point` of the next swap (the first time it gets there)."""

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


def _clear_hooks(rig: GatewayRig) -> None:
    rig.key_port.before_copy = None
    rig.key_port.on_staged_write = None
    rig.key_port.before_swap = None
    rig.key_port.after_swap = None
    rig.store.on_advance = None


def _watch_readers(rig: GatewayRig) -> list[tuple[Keys, ...]]:
    """Record what a reader would see at every step of the next apply."""
    seen: list[tuple[Keys, ...]] = []

    def record(*_: object) -> None:
        seen.append(_state(rig))

    rig.key_port.on_staged_write = record
    rig.key_port.before_swap = record
    rig.key_port.after_swap = record
    return seen


def _wait_until_reconciler_retires(rig: GatewayRig) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while rig.gateway.is_reconciling and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not rig.gateway.is_reconciling


def _clean_swap_digests() -> tuple[str, ...]:
    """Digests of the three graphs after the set was swapped in without any trouble."""
    rig = _seeded()
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    return digests(rig)


# -- the interruption table --------------------------------------------------------


@pytest.mark.parametrize("point", _CRASH_POINTS)
def test_recover_completes_a_swap_that_died_at_any_step_and_readers_see_none_or_all(
    point: str,
) -> None:
    rig = _seeded()
    _crash_at(rig, point)
    with pytest.raises(Crash):
        rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    assert _state(rig) in {_OLD_THREE, _MERGED_THREE}  # whatever the crash left, no mix
    _clear_hooks(rig)
    rig.restart()
    seen = _watch_readers(rig)
    behind = rig.store.graphs_with_pending_entries()

    result = rig.gateway.recover()

    assert result == RecoveryResult(recovered=behind, gated=())  # a leg already at its head is not
    assert set(behind) == (set(THREE) - {"baseline"} if point == "first_marker" else set(THREE))
    assert all(state in {_OLD_THREE, _MERGED_THREE} for state in seen)
    assert _state(rig) == _MERGED_THREE
    assert digests(rig) == _clean_swap_digests()
    assert rig.store.markers == {"baseline": 4, "native": 2, "policy_system": 3}
    assert staged_keys(rig) == []
    assert all(rig.gateway.is_caught_up(graph) for graph in THREE)


def test_a_staged_key_a_dead_process_left_is_replaced_not_reused() -> None:
    rig = _seeded()
    writes = 0

    def die_on_the_second_write() -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise Crash

    rig.key_port.on_staged_write = die_on_the_second_write
    with pytest.raises(Crash):
        rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    left_behind = staged_keys(rig)
    assert left_behind  # a half-built staged copy is still there
    _clear_hooks(rig)
    rig.restart()

    rig.gateway.recover()

    assert any(name in names for names in rig.key_port.discards for name in left_behind)
    assert _state(rig) == _MERGED_THREE
    assert digests(rig) == _clean_swap_digests()
    assert staged_keys(rig) == []


def test_startup_replay_completes_a_swap_that_died_after_the_rename() -> None:
    rig = _seeded()
    _crash_at(rig, "after_swap")
    with pytest.raises(Crash):
        rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    _clear_hooks(rig)
    rig.restart()

    report = rig.gateway.startup_replay()

    assert report.gated == ()
    assert _state(rig) == _MERGED_THREE
    assert rig.store.markers == {"baseline": 4, "native": 2, "policy_system": 3}


def test_a_leg_whose_key_was_lost_is_replayed_and_the_others_are_swapped() -> None:
    rig = _seeded()
    rig.key_port.copy_fault = Fault(_DOWN)  # the swap never gets going: committed, apply pending
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    rig.key_port.copy_fault = None
    rig.graphs.open("native").flush()  # the native key is lost
    rig.restart()

    report = rig.gateway.startup_replay()

    assert report.replayed == ("native",)
    assert set(report.caught_up) == {"baseline", "policy_system"}
    (swap,) = rig.key_port.swaps
    assert {live for _, live in swap} == {"baseline", "policy_system"}  # native was never staged
    assert report.gated == ()
    assert _state(rig) == _MERGED_THREE
    assert digests(rig) == _clean_swap_digests()
    assert rig.store.markers == {"baseline": 4, "native": 2, "policy_system": 3}


# -- graph store down for the whole apply, reconciler completes the unit -----------------------


def test_a_set_whose_apply_cannot_start_refuses_writers_and_the_reconciler_completes_it() -> None:
    stepped = SteppedWait(timeout=WAIT_SECONDS)
    rig = _seeded(reconciler_wait=stepped)
    rig.key_port.copy_fault = Fault(_DOWN)
    seen = _watch_readers(rig)

    outcome = rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert outcome.status == "committed_apply_pending"
    stepped.await_wait()
    assert _state(rig) == _OLD_THREE
    logged = [rig.store.logged_count(graph) for graph in THREE]
    for graph in THREE:  # no leg takes a write while the set is pending, nothing is logged
        with pytest.raises(GraphUnavailableError):
            rig.gateway.submit_group(MutationGroup(graph=graph, primitives=(node("late"),)))
    assert [rig.store.logged_count(graph) for graph in THREE] == logged
    rig.key_port.copy_fault = None

    stepped.resume()

    _wait_until_reconciler_retires(rig)
    assert _state(rig) == _MERGED_THREE
    assert all(state in {_OLD_THREE, _MERGED_THREE} for state in seen)
    assert rig.store.markers == {"baseline": 4, "native": 2, "policy_system": 3}
    stepped.stopped.set()
    stepped.resume()


# -- the write-path guard and unit-aware catch-up (F-1) ----------------------------------------


def test_the_write_path_guard_refuses_a_pending_set_without_taking_a_second_lock() -> None:
    rig = _seeded()
    rig.key_port.copy_fault = Fault(_DOWN)
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    rig.key_port.copy_fault = None
    copies = len(rig.key_port.copies)
    other_leg_lock = rig.gateway._locks.lock_for("baseline")  # pyright: ignore[reportPrivateUsage]
    group = MutationGroup(graph="native", primitives=(node("late"),))

    with other_leg_lock:  # a second lock would block here, the guard must not ask for it
        writer = Thread(lambda: rig.gateway.submit_group(group)).start()
        assert writer.finishes_within(WAIT_SECONDS)

    assert isinstance(writer.error, GraphUnavailableError)
    assert len(rig.key_port.copies) == copies  # refused, nothing partially applied
    assert _state(rig) == _OLD_THREE
    assert rig.store.logged_count("native") == 2


@pytest.mark.parametrize("leg_to_catch_up", THREE)
def test_catch_up_of_any_one_leg_completes_the_whole_pending_set(leg_to_catch_up: str) -> None:
    rig = _seeded()
    rig.key_port.copy_fault = Fault(_DOWN)
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    rig.key_port.copy_fault = None

    catcher = Thread(lambda: rig.gateway.catch_up(leg_to_catch_up)).start()

    assert catcher.finishes_within(WAIT_SECONDS)  # does not block on the locks it already holds
    assert catcher.error is None
    assert _state(rig) == _MERGED_THREE
    assert rig.store.markers == {"baseline": 4, "native": 2, "policy_system": 3}
    assert rig.gateway.gated_graphs() == ()


def test_catch_up_runs_alongside_a_swap_set_that_is_mid_append_without_deadlock() -> None:
    """A-1 (7a): a single-leg catch-up and a three-leg swap submit, opposite orders, a barrier."""
    rig = swap_rig()
    gate = Gate()
    rig.store.append_gate = gate
    submit = Thread(lambda: rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))).start()
    assert gate.entered.wait(WAIT_SECONDS)
    catch_up = Thread(lambda: rig.gateway.catch_up("policy_system")).start()

    blocked = not catch_up.finishes_within(BLOCKED_PROBE_SECONDS)
    gate.release.set()

    assert blocked
    assert submit.finishes_within(WAIT_SECONDS)
    assert catch_up.finishes_within(WAIT_SECONDS)
    assert (submit.error, catch_up.error) == (None, None)
    assert rig.store.markers == {"baseline": 3, "native": 1, "policy_system": 2}
    assert len(rig.key_port.swaps) == 1


def test_two_catch_ups_on_different_legs_of_one_pending_set_swap_it_once() -> None:
    rig = _seeded()
    rig.key_port.copy_fault = Fault(_DOWN)
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    rig.key_port.copy_fault = None
    gate = Gate()
    rig.key_port.before_swap = lambda: gate("baseline")
    first = Thread(lambda: rig.gateway.catch_up("baseline")).start()
    assert gate.entered.wait(WAIT_SECONDS)
    second = Thread(lambda: rig.gateway.catch_up("native")).start()

    second_waits = not second.finishes_within(BLOCKED_PROBE_SECONDS)
    gate.release.set()

    assert second_waits  # the unit's locks are held by the first
    assert first.finishes_within(WAIT_SECONDS)
    assert second.finishes_within(WAIT_SECONDS)
    assert (first.error, second.error) == (None, None)
    assert len(rig.key_port.swaps) == 1
    assert _state(rig) == _MERGED_THREE


def test_a_plain_prefix_in_front_of_a_swap_group_is_applied_in_place_first() -> None:
    rig = swap_rig()
    rig.store.append_groups_standalone(
        (_draft("baseline", "prefix"), _draft("native", "prefix")), as_swap_set=False
    )
    rig.store.append_groups_standalone(
        (_draft("baseline", "swapped"), _draft("native", "swapped")), as_swap_set=True
    )

    rig.gateway.catch_up("native")

    assert live_nodes(rig, "native") == {("Capability", "prefix"), ("Capability", "swapped")}
    assert live_nodes(rig, "baseline") == {("Capability", "prefix"), ("Capability", "swapped")}
    assert len(rig.key_port.swaps) == 1
    assert rig.store.markers == {"baseline": 2, "native": 2}


def _draft(graph: str, node_id: str) -> GraphLogGroupDraft:
    return GraphLogGroupDraft(graph=graph, entries=(encode_primitive(node(node_id)),))


def test_a_set_that_lands_after_the_unit_was_resolved_is_picked_up_by_resolving_again() -> None:
    rig = swap_rig()
    rig.gateway.submit_group(MutationGroup(graph="native", primitives=(node("first"),)))

    def a_set_lands() -> None:  # between the read and the locks: the stale unit is only `native`
        rig.store.append_groups_standalone(
            (_draft("baseline", "late"), _draft("native", "late")), as_swap_set=True
        )

    rig.store.after_pending_swap_read = a_set_lands

    result = rig.gateway.catch_up("native")

    assert result.caught_up
    assert live_nodes(rig, "baseline") == {("Capability", "late")}
    assert live_nodes(rig, "native") == {("Capability", "first"), ("Capability", "late")}
    assert len(rig.key_port.swaps) == 1  # one swap of the whole set, not one per graph


# -- the store seams ---------------------------------------------------------------------------


def test_the_store_finds_the_legs_of_a_swap_set_and_the_first_pending_one() -> None:
    rig = _seeded()
    rig.key_port.copy_fault = Fault(_DOWN)
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    (set_id,) = {group.swap_set_id for group in rig.store.groups if group.swap_set_id}

    legs = rig.store.read_swap_set(set_id)

    assert [(ref.graph, ref.first_position, ref.last_position) for ref in legs] == [
        ("baseline", 2, 4),
        ("native", 2, 2),
        ("policy_system", 2, 3),
    ]
    pending = rig.store.read_pending_swap_group("native", 1)
    assert pending is not None
    assert (pending.swap_set_id, pending.first_position) == (set_id, 2)
    assert rig.store.read_pending_swap_group("native", 2) is None  # applied through the group
    assert rig.store.read_pending_swap_group("elsewhere", 0) is None


# -- a blocked set (F-7) -----------------------------------------------------------------------


def _blocked_rig() -> GatewayRig:
    rig = _seeded()
    rig.key_port.swap_conflicts = 100
    outcome = rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    assert outcome.status == "committed_apply_pending"
    return rig


def test_a_blocked_set_refuses_writes_to_every_leg_and_logs_nothing() -> None:
    rig = _blocked_rig()
    logged = [rig.store.logged_count(graph) for graph in THREE]

    for graph in THREE:
        with pytest.raises(GraphApplyBlockedError):
            rig.gateway.submit_group(MutationGroup(graph=graph, primitives=(node("late"),)))

    assert [rig.store.logged_count(graph) for graph in THREE] == logged
    assert rig.gateway.gated_graphs() == THREE
    assert _state(rig) == _OLD_THREE


def test_catch_up_retries_a_blocked_set_and_it_succeeds_once_the_fault_clears() -> None:
    rig = _blocked_rig()
    still_blocked = rig.gateway.catch_up("native")
    assert still_blocked.caught_up is False
    rig.key_port.swap_conflicts = 0

    result = rig.gateway.catch_up("native")

    assert result.caught_up
    assert _state(rig) == _MERGED_THREE
    assert rig.gateway.gated_graphs() == ()
    assert digests(rig) == _clean_swap_digests()
    late = MutationGroup(graph="baseline", primitives=(node("late"),))
    assert rig.gateway.submit_group(late).status == "applied"


def test_replaying_each_leg_in_ascending_order_unblocks_the_set_with_the_swapped_digests() -> None:
    rig = _blocked_rig()

    rig.gateway.replay_graph("baseline")
    assert rig.gateway.gated_graphs() == THREE  # one leg at its head does not end the block
    rig.gateway.replay_graph("native")
    assert rig.gateway.gated_graphs() != ()
    rig.gateway.replay_graph("policy_system")

    assert rig.gateway.gated_graphs() == ()
    assert _state(rig) == _MERGED_THREE
    assert digests(rig) == _clean_swap_digests()
    assert rig.store.markers == {"baseline": 4, "native": 2, "policy_system": 3}


def test_a_staged_apply_the_graph_refused_is_retried_by_catch_up() -> None:
    rig = _seeded()

    def refuse(source: str, staged: str) -> None:
        if source == "native":
            rig.graphs.open(staged).fail_on_write(redis.exceptions.ResponseError("bad query"))

    rig.key_port.after_copy = refuse
    with pytest.raises(GraphApplyError):
        rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    rig.key_port.after_copy = None

    result = rig.gateway.catch_up("baseline")

    assert result.caught_up
    assert _state(rig) == _MERGED_THREE
    assert rig.gateway.gated_graphs() == ()


def test_a_gateway_without_a_key_port_cannot_complete_a_pending_swap_and_blocks_its_legs() -> None:
    rig = _seeded()
    rig.key_port.copy_fault = Fault(_DOWN)
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    rig.key_port.copy_fault = None
    rig.restart(with_port=False)

    result = rig.gateway.recover()

    assert result == RecoveryResult(recovered=(), gated=THREE)
    assert rig.gateway.gated_graphs() == THREE
    assert _state(rig) == _OLD_THREE
