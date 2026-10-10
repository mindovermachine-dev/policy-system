"""Startup replay finds the graphs whose projection is gone or half built (#207 S15, AC-RD-006).

Every test enters at `GraphWriteGateway.startup_replay`. A wiped FalkorDB graph cannot be seen
from the applied marker (which only moves forward and still says "applied"), so each graph is
classified by probing FalkorDB itself: the progress sentinel first, then whether the graph holds
any node.
"""

from __future__ import annotations

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import EMPTY_GRAPH_DIGEST, canonical_digest
from ps_service.graph_gateway.errors import GraphReplayGatedError
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import (
    DeleteNode,
    DigestCheckpoint,
    MutationGroup,
    Primitive,
    StartupReplayReport,
    UpsertNode,
)
from ps_service.graph_gateway.replay_state import ReplayState, read_replay_state, write_replay_state

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_OTHER = "policy_system"
_DOWN = redis.exceptions.ConnectionError("falkordb is down")


def _node(node_id: str) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": node_id})


def _submit(
    rig: GatewayRig, *primitives: Primitive, graph: str = _GRAPH, checkpoint: bool = False
) -> None:
    rig.gateway.submit_group(
        MutationGroup(
            graph=graph,
            audit_event_id=_AUDIT_EVENT_ID,
            primitives=primitives,
            checkpoint_requested=checkpoint,
        )
    )


def _data_queries(rig: GatewayRig, graph: str = _GRAPH) -> int:
    return sum(1 for q in rig.graphs.open(graph).queries if q.kind in {"write", "index"})


def test_startup_replay_rebuilds_a_wiped_graph_whose_marker_says_applied() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"), _node("cap-2"), checkpoint=True)
    expected = canonical_digest(rig.graphs.open(_GRAPH))
    rig.graphs.open(_GRAPH).flush()
    assert rig.store.read_applied_position(_GRAPH) == 2

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport(replayed=(_GRAPH,))
    assert canonical_digest(rig.graphs.open(_GRAPH)) == expected


def test_startup_replay_detects_a_graph_key_that_does_not_exist_as_empty() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"), checkpoint=True)
    del rig.graphs.graphs[_GRAPH]  # FalkorDB lost the key altogether

    report = rig.restart().startup_replay()

    assert report.replayed == (_GRAPH,)
    assert set(rig.graphs.open(_GRAPH).nodes) == {("Capability", "cap-1")}


def test_startup_replay_leaves_a_healthy_graph_untouched() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"), checkpoint=True)
    writes_before = _data_queries(rig)
    markers_before = list(rig.store.marker_history)

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport(untouched=(_GRAPH,))
    assert _data_queries(rig) == writes_before  # only the probes read
    assert rig.store.marker_history == markers_before


def test_startup_replay_catches_up_a_non_empty_graph_that_is_behind_its_log() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"))
    rig.graphs.open(_GRAPH).fail_on_write(_DOWN)
    _submit(rig, _node("cap-2"))  # committed, apply pending
    rig.graphs.open(_GRAPH).heal()

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport(caught_up=(_GRAPH,))
    assert set(rig.graphs.open(_GRAPH).nodes) == {("Capability", "cap-1"), ("Capability", "cap-2")}


def test_startup_replay_ignores_a_graph_with_no_log_entries() -> None:
    rig = GatewayRig()

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport()
    assert rig.graphs.graphs == {}  # nothing was even opened


def test_startup_replay_treats_every_wiped_graph_independently() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"), graph=_GRAPH, checkpoint=True)
    _submit(rig, _node("pol-1"), graph=_OTHER, checkpoint=True)
    rig.graphs.open(_GRAPH).flush()  # only one of them is wiped

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport(replayed=(_GRAPH,), untouched=(_OTHER,))


def test_startup_replay_resumes_a_graph_with_an_in_progress_sentinel() -> None:
    rig = GatewayRig(settings=GatewaySettings(replay_page_size=2, batch_size=1))
    for number in range(1, 7):
        _submit(rig, _node(f"cap-{number}"))
    expected = canonical_digest(rig.graphs.open(_GRAPH))
    graph = rig.graphs.open(_GRAPH)
    graph.flush()
    graph.fail_after_n_writes(3, _DOWN)  # dies inside the second page
    with pytest.raises(redis.exceptions.ConnectionError):
        rig.restart().replay_graph(_GRAPH)
    graph.heal()
    assert (
        rig.store.read_applied_position(_GRAPH) == 6
    )  # the marker says applied, the graph is partial

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport(resumed=(_GRAPH,))
    assert canonical_digest(graph) == expected
    assert read_replay_state(graph) is None


def test_one_graph_failing_replay_does_not_stop_the_others_and_is_reported_gated() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"), graph=_GRAPH, checkpoint=True)
    _submit(rig, _node("pol-1"), graph=_OTHER, checkpoint=True)
    rig.store.checkpoints[(_GRAPH, 1)] = DigestCheckpoint(
        graph=_GRAPH, position=1, canonical_digest="sha256:" + "00" * 32
    )
    rig.graphs.open(_GRAPH).flush()
    rig.graphs.open(_OTHER).flush()
    gateway = rig.restart()

    report = gateway.startup_replay()

    assert report == StartupReplayReport(replayed=(_OTHER,), gated=(_GRAPH,))
    with pytest.raises(GraphReplayGatedError):
        _submit(rig, _node("cap-2"), graph=_GRAPH)
    _submit(rig, _node("pol-2"), graph=_OTHER)  # the other graph accepts writes again


def test_a_graph_recorded_as_failed_stays_gated_without_applying_anything() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"))
    graph = rig.graphs.open(_GRAPH)
    write_replay_state(graph, ReplayState(position=1, state="failed", kind="X", verified=-1))
    writes_before = _data_queries(rig)

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport(gated=(_GRAPH,))
    assert _data_queries(rig) == writes_before
    assert read_replay_state(graph) is not None  # the record is kept


def test_no_graph_accepts_a_write_while_the_startup_replay_runs_then_all_but_failed_do() -> None:
    rig = GatewayRig(settings=GatewaySettings(replay_page_size=1))
    _submit(rig, _node("cap-1"), _node("cap-2"), graph=_GRAPH)
    rig.graphs.open(_GRAPH).flush()
    gateway = rig.restart()
    refused: list[str] = []

    def write_to_a_graph_that_is_not_logged() -> None:
        try:
            gateway.submit_group(
                MutationGroup(
                    graph="never_logged",
                    audit_event_id=_AUDIT_EVENT_ID,
                    primitives=(_node("x"),),
                )
            )
        except GraphReplayGatedError:
            refused.append("never_logged")

    rig.store.on_paged_read = write_to_a_graph_that_is_not_logged

    gateway.startup_replay()

    assert refused  # while the replay runs, no graph is open for writes
    rig.store.on_paged_read = None
    gateway.submit_group(
        MutationGroup(
            graph="never_logged", audit_event_id=_AUDIT_EVENT_ID, primitives=(_node("x"),)
        )
    )  # and afterwards every graph that did not fail is


# The empty-graph rule: a log that nets to empty is healthy, anything else is rebuilt.


def test_a_graph_whose_log_nets_to_empty_with_a_head_checkpoint_is_healthy_and_not_replayed() -> (
    None
):
    rig = GatewayRig()
    _submit(rig, _node("cap-1"))
    _submit(rig, DeleteNode(label="Capability", id="cap-1"), checkpoint=True)
    assert rig.store.checkpoints[(_GRAPH, 2)].canonical_digest == EMPTY_GRAPH_DIGEST
    writes_before = _data_queries(rig)

    report = rig.restart().startup_replay()

    assert report == StartupReplayReport(untouched=(_GRAPH,))
    assert _data_queries(rig) == writes_before


def test_an_empty_graph_whose_log_does_not_net_to_empty_is_replayed() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"))
    _submit(rig, _node("cap-2"), checkpoint=True)
    rig.graphs.open(_GRAPH).flush()

    report = rig.restart().startup_replay()

    assert report.replayed == (_GRAPH,)
    assert len(rig.graphs.open(_GRAPH).nodes) == 2


def test_an_empty_graph_with_a_head_checkpoint_of_a_non_empty_digest_is_replayed() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"), checkpoint=True)
    rig.graphs.open(_GRAPH).flush()
    assert rig.store.checkpoints[(_GRAPH, 1)].canonical_digest != EMPTY_GRAPH_DIGEST

    report = rig.restart().startup_replay()

    assert report.replayed == (_GRAPH,)


def test_an_empty_graph_whose_log_nets_to_empty_but_has_no_head_checkpoint_is_replayed() -> None:
    rig = GatewayRig()
    _submit(rig, _node("cap-1"))
    _submit(rig, DeleteNode(label="Capability", id="cap-1"))  # nothing vouches for the head

    report = rig.restart().startup_replay()

    assert report.replayed == (_GRAPH,)


def test_the_empty_graph_digest_is_the_digest_of_a_graph_with_no_element() -> None:
    rig = GatewayRig()

    assert canonical_digest(rig.graphs.open(_GRAPH)) == EMPTY_GRAPH_DIGEST
