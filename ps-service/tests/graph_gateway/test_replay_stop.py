"""A replay can be asked to stop and stops between pages, resumable (#207 S12b, AC-RD-005/006)."""

from __future__ import annotations

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import GraphReplayGatedError, GraphReplayStoppedError
from ps_service.graph_gateway.gateway import GatewaySettings
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.graph_gateway.replay_state import read_replay_state

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_SETTINGS = GatewaySettings(replay_page_size=4)


def _built_rig() -> tuple[GatewayRig, str]:
    rig = GatewayRig(settings=_SETTINGS)
    for number in range(1, 13):
        rig.gateway.submit_group(
            MutationGroup(
                graph=_GRAPH,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=(
                    UpsertNode(label="Capability", id=f"cap-{number}", properties={"n": number}),
                ),
            )
        )
    digest = canonical_digest(rig.graphs.open(_GRAPH))
    rig.graphs.open(_GRAPH).flush()
    return rig, digest


def test_replay_stop_event_stops_between_pages_and_leaves_a_resumable_sentinel() -> None:
    rig, expected = _built_rig()
    gateway = rig.restart()
    rig.store.on_paged_read = gateway.stop_replay  # the shutdown arrives while page one is read

    with pytest.raises(GraphReplayStoppedError) as raised:
        gateway.replay_graph(_GRAPH)

    graph = rig.graphs.open(_GRAPH)
    state = read_replay_state(graph)
    assert state is not None
    assert (state.position, state.state) == (4, "in_progress")  # page one was applied whole
    assert (raised.value.graph, raised.value.position) == (_GRAPH, 4)
    assert rig.store.read_applied_position(_GRAPH) == 12  # the marker was never rewound or moved
    rig.store.on_paged_read = None

    report = rig.restart().replay_graph(_GRAPH)

    assert report.resumed_from == 5
    assert canonical_digest(graph) == expected


def test_a_stopped_replay_leaves_the_graph_gated_not_failed() -> None:
    rig, _ = _built_rig()
    gateway = rig.restart()
    rig.store.on_paged_read = gateway.stop_replay
    with pytest.raises(GraphReplayStoppedError):
        gateway.replay_graph(_GRAPH)

    with pytest.raises(GraphReplayGatedError) as raised:
        gateway.submit_group(
            MutationGroup(
                graph=_GRAPH,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=(UpsertNode(label="Capability", id="late", properties={}),),
            )
        )

    assert "replay has not completed" in str(raised.value)


def test_a_stop_requested_before_the_replay_starts_applies_nothing() -> None:
    rig, _ = _built_rig()
    gateway = rig.restart()
    gateway.stop_replay()
    graph = rig.graphs.open(_GRAPH)
    writes_before = sum(1 for query in graph.queries if query.kind == "write")

    with pytest.raises(GraphReplayStoppedError):
        gateway.replay_graph(_GRAPH)

    assert sum(1 for query in graph.queries if query.kind == "write") == writes_before
