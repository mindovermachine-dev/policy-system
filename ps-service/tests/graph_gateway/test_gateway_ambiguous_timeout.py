"""A write timeout whose request the graph still executed is retried to the same final graph.

The fake graph applies the write and then raises the timeout, which is what a lost reply looks
like to the gateway. The retry sends the same unmarked run again, so every primitive must land
exactly once. A swap whose copy or rename timed out must leave the live graphs unchanged, or
complete to the same digests once the store answers again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import Fault, GatewayRig, InMemoryGraph
from graph_gateway._swap_support import (
    EMPTY_THREE,
    THREE,
    digests,
    live_nodes,
    new_three,
    staged_keys,
    swap_rig,
    swap_set,
    three_legs,
)
from ps_service.graph_gateway.errors import GraphApplyError
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    MutationGroup,
    NodeRef,
    Primitive,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging import LogEmitter

    MakeEmitter = Callable[[], tuple[LogEmitter, Path]]
    ReadLines = Callable[[Path], list[dict[str, object]]]

_GRAPH = "compliance"
_LOST_REPLY = redis.exceptions.TimeoutError("Timeout reading from socket")


def _node(node_id: str) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": node_id})


def _edge(identity: str) -> UpsertEdge:
    return UpsertEdge(
        type="HAS",
        identity=identity,
        source=NodeRef(label="Policy", id="p-1"),
        target=NodeRef(label="Standard", id="s-1"),
        properties={"weight": 1},
    )


def _group(*primitives: Primitive) -> MutationGroup:
    return MutationGroup(graph=_GRAPH, primitives=primitives)


def _seeded(*primitives: Primitive) -> tuple[GatewayRig, InMemoryGraph]:
    rig = GatewayRig()
    rig.gateway.submit_group(
        _group(
            UpsertNode(label="Policy", id="p-1", properties={"name": "p"}),
            UpsertNode(label="Standard", id="s-1", properties={"name": "s"}),
            *primitives,
        )
    )
    rig.sleeps.clear()
    return rig, rig.graphs.open(_GRAPH)


def test_a_node_upsert_run_whose_reply_was_lost_leaves_each_node_exactly_once() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)
    graph.apply_then_raise(_LOST_REPLY)

    outcome = rig.gateway.submit_group(_group(_node("cap-1"), _node("cap-2")))

    assert outcome.status == "applied"
    assert set(graph.nodes) == {("Capability", "cap-1"), ("Capability", "cap-2")}
    assert rig.store.logged_count(_GRAPH) == 2
    assert rig.store.markers[_GRAPH] == 2
    assert rig.sleeps == [0.2]


def test_an_edge_upsert_run_whose_reply_was_lost_leaves_one_edge() -> None:
    rig, graph = _seeded()
    graph.apply_then_raise(_LOST_REPLY)

    outcome = rig.gateway.submit_group(_group(_edge("e-1")))

    assert outcome.status == "applied"
    assert len(graph.edges) == 1
    assert rig.sleeps == [0.2]


def test_a_delete_run_whose_reply_was_lost_leaves_the_graph_without_the_elements() -> None:
    rig, graph = _seeded(_node("cap-1"), _edge("e-1"))
    graph.apply_then_raise(_LOST_REPLY)

    outcome = rig.gateway.submit_group(
        _group(
            DeleteEdge(
                type="HAS",
                identity="e-1",
                source=NodeRef(label="Policy", id="p-1"),
                target=NodeRef(label="Standard", id="s-1"),
            ),
            DeleteNode(label="Capability", id="cap-1"),
        )
    )

    assert outcome.status == "applied"
    assert graph.edges == {}
    assert ("Capability", "cap-1") not in graph.nodes
    assert rig.sleeps == [0.2]


def test_a_mixed_group_whose_reply_was_lost_matches_a_fault_free_submission() -> None:
    faulty, graph = _seeded()
    clean, _ = _seeded()
    graph.apply_then_raise(_LOST_REPLY)
    mixed = _group(_node("cap-1"), _edge("e-1"), _node("cap-2"))

    faulty.gateway.submit_group(mixed)
    clean.gateway.submit_group(mixed)

    assert graph.nodes == clean.graphs.open(_GRAPH).nodes
    assert graph.edges == clean.graphs.open(_GRAPH).edges
    assert faulty.store.logged_count(_GRAPH) == clean.store.logged_count(_GRAPH)
    assert faulty.store.markers == clean.store.markers


def test_a_timeout_on_the_last_run_resends_only_that_run(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    rig.gateway.submit_group(
        _group(
            UpsertNode(label="Policy", id="p-1", properties={"name": "p"}),
            UpsertNode(label="Standard", id="s-1", properties={"name": "s"}),
        )
    )
    graph = rig.graphs.open(_GRAPH)
    writes = {"count": 0}

    def lose_the_reply_of_the_second_write() -> None:
        writes["count"] += 1
        if writes["count"] == 2:
            graph.apply_then_raise(_LOST_REPLY)

    graph.queries.clear()
    graph.on_write = lose_the_reply_of_the_second_write

    outcome = rig.gateway.submit_group(_group(_node("cap-1"), _edge("e-1")))
    emitter.flush()

    sent = [query.template for query in graph.queries if query.kind == "write"]
    assert outcome.status == "applied"
    assert sent == ["upsert_node", "upsert_edge", "upsert_edge"]
    assert len(graph.edges) == 1
    retries = [line for line in read_lines(log_path) if line.get("action") == "apply_retry"]
    assert [line["error_class"] for line in retries] == ["TimeoutError"]


def test_a_copy_whose_reply_was_lost_leaves_the_live_graphs_unchanged_and_no_staged_key() -> None:
    rig = swap_rig()
    before = digests(rig)

    def lose_the_reply_once(source: str, staged: str) -> None:
        del source, staged
        rig.key_port.after_copy = None
        raise _LOST_REPLY

    rig.key_port.after_copy = lose_the_reply_once

    with pytest.raises(GraphApplyError):
        rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert tuple(live_nodes(rig, graph) for graph in THREE) == EMPTY_THREE
    assert digests(rig) == before
    assert staged_keys(rig) == []
    assert rig.store.markers == {}
    assert rig.key_port.swaps == []


def test_a_set_whose_copy_reply_was_lost_completes_to_the_fault_free_digests_afterwards() -> None:
    rig = swap_rig()
    clean = swap_rig()
    clean.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    def lose_the_reply_once(source: str, staged: str) -> None:
        del source, staged
        rig.key_port.after_copy = None
        raise _LOST_REPLY

    rig.key_port.after_copy = lose_the_reply_once
    with pytest.raises(GraphApplyError):
        rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    rig.gateway.catch_up("baseline")

    assert tuple(live_nodes(rig, graph) for graph in THREE) == new_three()
    assert digests(rig) == digests(clean)
    assert staged_keys(rig) == []


def test_a_rename_that_timed_out_before_it_ran_is_not_retried_and_changes_nothing() -> None:
    rig = swap_rig()
    before = digests(rig)
    rig.key_port.swap_fault = Fault(_LOST_REPLY, times=1)

    outcome = rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    assert outcome.status == "committed_apply_pending"
    assert len(rig.key_port.watches) == 1
    assert tuple(live_nodes(rig, graph) for graph in THREE) == EMPTY_THREE
    assert digests(rig) == before
    assert staged_keys(rig) == []
    assert rig.key_port.swaps == []
    assert rig.store.markers == {}


def test_a_rename_that_ran_but_whose_reply_was_lost_converges_to_the_fault_free_digests() -> None:
    rig = swap_rig()
    clean = swap_rig()
    clean.gateway.submit_multi_graph_group(swap_set(*three_legs()))

    def lose_the_reply_once() -> None:
        rig.key_port.after_swap = None
        raise _LOST_REPLY

    rig.key_port.after_swap = lose_the_reply_once
    outcome = rig.gateway.submit_multi_graph_group(swap_set(*three_legs()))
    assert outcome.status == "committed_apply_pending"
    assert len(rig.key_port.watches) == 1  # the rename is never asked twice

    rig.restart()
    report = rig.gateway.startup_replay()

    assert report.gated == ()
    assert digests(rig) == digests(clean)
    assert staged_keys(rig) == []
    assert rig.store.markers == {"baseline": 3, "native": 1, "policy_system": 2}
