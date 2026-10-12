"""A multi-graph group applied by swap (issue #214 S4, AC-BI-003, AC-BI-001 replay equality).

Every test enters at `GraphWriteGateway.submit_multi_graph_group(apply="swap")` against the
Protocol fakes. The in-memory key port records every copy, discard and rename and offers hooks at
the observation points, so "the live graphs are untouched until the flip" and "the flip changes all
legs together" are asserted at the points where a reader could look. AC-BI-003 is protocol-only
here (true by construction on the fake); it is closed live in S6 and S8b.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import Fault
from graph_gateway._swap_support import (
    EMPTY_THREE,
    THREE,
    digests,
    leg,
    live_nodes,
    new_three,
    node,
    staged_keys,
    swap_rig,
    swap_set,
    three_legs,
)
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import (
    GraphApplyBlockedError,
    GraphApplyError,
    SwapNotConfiguredError,
)
from ps_service.graph_gateway.models import ExpectedPosition, MutationGroup

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Protocol

    from graph_gateway._fakes import Keys
    from ps_service.logging import LogEmitter

    class MakeEmitter(Protocol):
        """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

        def __call__(self) -> tuple[LogEmitter, Path]: ...

    class ReadLines(Protocol):
        """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

        def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


_DOWN = redis.exceptions.ConnectionError("Error 111 connecting to 10.1.2.3:6379.")


# -- the happy path: staged copies, one flip, markers after the flip ---------------------------


def test_live_graphs_stay_untouched_while_the_staged_copies_are_built() -> None:
    rig = swap_rig()
    seen: list[tuple[Keys, ...]] = []
    rig.key_port.on_staged_write = lambda: seen.append(rig.key_port.observe(*THREE))

    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert seen  # the staged copies really were written to
    assert all(state == EMPTY_THREE for state in seen)


def test_the_flip_changes_all_legs_together_and_the_markers_follow_it() -> None:
    rig = swap_rig()
    before_flip: list[tuple[tuple[Keys, ...], dict[str, int]]] = []
    after_flip: list[tuple[tuple[Keys, ...], dict[str, int]]] = []
    rig.key_port.before_swap = lambda: before_flip.append(
        (rig.key_port.observe(*THREE), dict(rig.store.markers))
    )
    rig.key_port.after_swap = lambda: after_flip.append(
        (rig.key_port.observe(*THREE), dict(rig.store.markers))
    )

    outcome = rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert before_flip == [(EMPTY_THREE, {})]  # all old, no marker moved
    assert after_flip == [(new_three(), {})]  # all new at once, markers still behind the rename
    assert outcome.status == "applied"
    assert {leg_outcome.status for leg_outcome in outcome.legs} == {"applied"}
    assert rig.store.markers == {"baseline": 3, "native": 1, "policy_system": 2}
    assert rig.key_port.swaps == [
        tuple((f"{graph}__swap__{rig.store.groups[0].swap_set_id}", graph) for graph in THREE)
    ]


def test_a_swap_ends_with_the_same_graphs_as_applying_the_set_in_place() -> None:
    swapped, in_place = swap_rig(), swap_rig()
    seed = swap_set(
        leg("native", node("old"), node("n-1")),
        leg("baseline", node("old")),
        apply="in_place",
    )
    replacing = (
        leg("native", node("n-1", name="renamed"), replace=True),
        leg("baseline", node("b-1"), replace=True),
        leg("policy_system", node("p-1")),
    )
    for rig in (swapped, in_place):
        rig.gateway.submit_multi_graph_group(seed)
    swapped.gateway.submit_multi_graph_group(swap_set(*replacing))
    in_place.gateway.submit_multi_graph_group(swap_set(*replacing, apply="in_place"))

    assert digests(swapped) == digests(in_place)
    assert live_nodes(swapped, "native") == {("Capability", "n-1")}  # `old` was replaced away


def test_a_requested_checkpoint_is_taken_after_the_flip_at_the_legs_last_position() -> None:
    rig = swap_rig()
    seen: list[dict[tuple[str, int], object]] = []
    rig.key_port.after_swap = lambda: seen.append(dict(rig.store.checkpoints))

    outcome = rig.gateway.submit_multi_graph_group(
        swap_set(
            leg("native", node("n-1"), checkpoint_requested=True),
            leg("baseline", node("b-1"), node("b-2")),
        )
    )

    assert seen == [{}]  # none taken before the flip
    by_graph = {leg_outcome.graph: leg_outcome for leg_outcome in outcome.legs}
    assert (by_graph["native"].checkpoint, by_graph["native"].checkpoint_position) == (
        "recorded",
        1,
    )
    assert by_graph["baseline"].checkpoint == "not_requested"
    assert set(rig.store.checkpoints) == {("native", 1)}
    assert rig.store.checkpoints[("native", 1)].canonical_digest == canonical_digest(
        rig.graphs.open("native")
    )


def test_the_id_indexes_exist_on_the_staged_key_before_the_first_load() -> None:
    rig = swap_rig()

    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    for graph in THREE:
        live = rig.graphs.open(graph)
        templates = [query.template for query in live.queries if query.kind in {"index", "write"}]
        assert "Capability" in live.indexes
        assert templates.index("create_index") < templates.index("upsert_node")


def test_the_staged_key_is_unique_to_the_set_and_gone_after_the_flip() -> None:
    rig = swap_rig()

    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs("-again")))

    first, second = (
        {group.swap_set_id for group in rig.store.groups[:3]},
        {group.swap_set_id for group in rig.store.groups[3:]},
    )
    assert len(first) == len(second) == 1
    assert first != second
    (first_id,), (second_id,) = first, second
    assert {destination for _, destination in rig.key_port.copies} == {
        f"{graph}__swap__{set_id}" for set_id in (first_id, second_id) for graph in THREE
    }
    assert staged_keys(rig) == []


def test_a_set_applied_in_place_is_not_a_swap_set() -> None:
    rig = swap_rig()

    rig.gateway.submit_multi_graph_group(swap_set(*three_legs(), apply="in_place"))

    assert {group.swap_set_id for group in rig.store.groups} == {None}
    assert rig.key_port.copies == []


def test_a_leg_that_changes_nothing_is_neither_logged_nor_swapped() -> None:
    rig = swap_rig()
    rig.gateway.submit_multi_graph_group(swap_set(leg("native"), leg("baseline"), apply="in_place"))

    outcome = rig.gateway.submit_multi_graph_group(
        swap_set(leg("native"), leg("baseline", node("b-new")), leg("policy_system"))
    )

    assert {leg_outcome.graph: leg_outcome.status for leg_outcome in outcome.legs} == {
        "baseline": "applied",
        "native": "unchanged",
        "policy_system": "applied",
    }
    swapped_live = {live for pairs in rig.key_port.swaps for _, live in pairs}
    assert swapped_live == {"baseline", "policy_system"}


def test_a_leg_whose_live_key_does_not_exist_yet_is_swapped_in_as_a_new_graph() -> None:
    rig = swap_rig()
    assert "native" not in rig.graphs.graphs

    rig.key_port.copy("native", "native__swap__x")

    assert set(rig.graphs.graphs) == {"native__swap__x"}  # an absent source copies as an empty key
    assert rig.graphs.graphs["native__swap__x"].nodes == {}


def test_replaying_each_leg_from_the_log_rebuilds_the_swapped_graph() -> None:
    rig = swap_rig()
    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    swapped = digests(rig)

    for graph in THREE:
        rig.graphs.open(graph).flush()
        rig.gateway.replay_graph(graph)

    assert digests(rig) == swapped
    assert live_nodes(rig, "baseline") == new_three()[0]


# -- the swap is retried, then blocks (F-7) ----------------------------------------------------


def test_an_aborted_watch_restarts_from_a_fresh_copy_and_then_succeeds() -> None:
    rig = swap_rig()
    rig.key_port.swap_conflicts = 1

    outcome = rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert outcome.status == "applied"
    assert len(rig.key_port.copies) == 6  # three legs copied twice
    assert live_nodes(rig, "native") == new_three()[1]
    assert staged_keys(rig) == []
    assert rig.key_port.discards  # the aborted attempt's staged keys were dropped


def test_a_swap_that_keeps_aborting_blocks_the_set_and_leaves_the_live_graphs_untouched() -> None:
    rig = swap_rig()
    rig.key_port.swap_conflicts = 100

    outcome = rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert outcome.status == "committed_apply_pending"
    assert {leg_outcome.status for leg_outcome in outcome.legs} == {"committed_apply_pending"}
    assert len(rig.key_port.copies) == 3 * 4  # max_attempts (4) copies per leg
    assert tuple(live_nodes(rig, graph) for graph in THREE) == EMPTY_THREE
    assert staged_keys(rig) == []
    assert rig.store.markers == {}
    for graph in THREE:  # every leg refuses writers, nothing is logged
        with pytest.raises(GraphApplyBlockedError):
            rig.gateway.submit_group(MutationGroup(graph=graph, primitives=(node("late"),)))
    assert [rig.store.logged_count(graph) for graph in THREE] == [3, 1, 2]


def test_a_staged_apply_the_graph_refuses_blocks_the_set_and_raises() -> None:
    rig = swap_rig()

    def refuse_writes(source: str, staged: str) -> None:
        if source == "native":
            rig.graphs.open(staged).fail_on_write(redis.exceptions.ResponseError("bad query"))

    rig.key_port.after_copy = refuse_writes

    with pytest.raises(GraphApplyError):
        rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert tuple(live_nodes(rig, graph) for graph in THREE) == EMPTY_THREE
    assert staged_keys(rig) == []
    assert rig.gateway.gated_graphs() == THREE
    assert rig.store.markers == {}


def test_a_graph_store_that_stays_down_leaves_the_set_committed_and_pending() -> None:
    rig = swap_rig()
    rig.key_port.copy_fault = Fault(_DOWN)

    outcome = rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert outcome.status == "committed_apply_pending"
    assert tuple(live_nodes(rig, graph) for graph in THREE) == EMPTY_THREE
    assert rig.store.markers == {}
    assert rig.gateway.is_reconciling
    assert len(rig.sleeps) == 3  # the retry budget (4 tries) was spent on the first copy


def test_a_gateway_without_a_key_port_refuses_a_swap_set_and_logs_nothing() -> None:
    rig = swap_rig(with_port=False)

    with pytest.raises(SwapNotConfiguredError):
        rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert [rig.store.logged_count(graph) for graph in THREE] == [0, 0, 0]


def test_a_stale_position_on_any_leg_still_rejects_the_swap_set_before_anything_is_staged() -> None:
    rig = swap_rig()

    with pytest.raises(Exception, match="expected"):
        rig.gateway.submit_multi_graph_group(
            swap_set(
                leg("native"),
                leg("baseline", preconditions=(ExpectedPosition(position=7),)),
            )
        )

    assert rig.key_port.copies == []
    assert [rig.store.logged_count(graph) for graph in THREE] == [0, 0, 0]


# -- semantic logging --------------------------------------------------------------------------


def test_a_swap_logs_one_content_free_entry_with_the_hold_time(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = swap_rig(emitter=emitter)
    secret = "sentinel-secret-payload"

    rig.gateway.submit_multi_graph_group(
        swap_set(leg("native", node("n", secret=secret)), leg("baseline", node("b")))
    )
    emitter.flush()

    lines = read_lines(log_path)
    (swapped,) = [line for line in lines if line.get("action") == "swap_set"]
    assert (swapped["component"], swapped["outcome"], swapped["leg_count"]) == (
        "graph_gateway",
        "success",
        2,
    )
    assert isinstance(swapped["hold_seconds"], float)
    assert swapped["hold_seconds"] >= 0
    assert swapped["audit_event_id"] == "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
    assert secret not in json.dumps(lines)


def test_a_blocked_swap_is_logged_with_the_error_class_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = swap_rig(emitter=emitter)
    rig.key_port.swap_conflicts = 100

    rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    emitter.flush()

    (failed,) = [
        line
        for line in read_lines(log_path)
        if line.get("action") == "swap_set" and line.get("outcome") == "failure"
    ]
    assert failed["error_class"] == "SwapConflictError"
