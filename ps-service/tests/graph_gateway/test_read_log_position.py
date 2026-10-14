"""`GraphWriteGateway.read_log_position` reads a graph's last log position and nothing else.

The narrow accessor behind a guarded group's `ExpectedPosition`: the guard needs the position, not
the full node and edge scan `read_persisted_elements` also does.
"""

from __future__ import annotations

import pytest

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.errors import (
    GraphLogGapError,
    GraphLogUnavailableError,
    GraphReplayError,
    GraphReplayGatedError,
)
from ps_service.graph_gateway.models import MutationGroup, UpsertNode

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "policy_system"


def _group(*node_ids: str) -> MutationGroup:
    return MutationGroup(
        graph=_GRAPH,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=tuple(UpsertNode(label="Policy", id=node_id) for node_id in node_ids),
    )


def test_a_fresh_graph_is_at_position_zero() -> None:
    rig = GatewayRig()

    assert rig.gateway.read_log_position(_GRAPH) == 0


def test_the_position_equals_the_last_logged_position_after_groups() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group("p-1", "p-2"))
    rig.gateway.submit_group(_group("p-3"))

    assert rig.gateway.read_log_position(_GRAPH) == 3 == rig.store.last_position(_GRAPH)


def test_an_unchanged_group_does_not_move_the_position() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group("p-1"))
    rig.gateway.submit_group(_group("p-1"))

    assert rig.gateway.read_log_position(_GRAPH) == 1


def test_the_position_counts_the_entries_of_a_legacy_edge_stamp() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group("p-1"))
    graph = rig.graphs.open(_GRAPH)
    graph.seed_node(("Standard",), "s-1", {})
    graph.seed_legacy_edge(("SUPPORTED_BY", "Policy", "p-1", "Standard", "s-1"), {})
    before = rig.gateway.read_log_position(_GRAPH)

    stamp = rig.gateway.stamp_legacy_edge_identity(_GRAPH, None)

    assert rig.gateway.read_log_position(_GRAPH) == before + stamp.stamped == 2


def test_the_read_scans_no_node_or_edge_and_writes_nothing() -> None:
    rig = GatewayRig()
    rig.gateway.submit_group(_group("p-1"))
    graph = rig.graphs.open(_GRAPH)
    queried, logged = len(graph.queries), len(rig.store.groups)

    rig.gateway.read_log_position(_GRAPH)

    assert (len(graph.queries), len(rig.store.groups)) == (queried, logged)


def test_a_graph_closed_by_a_failed_replay_refuses_the_read_as_read_persisted_elements_does() -> (
    None
):
    rig = GatewayRig()
    rig.gateway.submit_group(_group("p-1"))
    rig.gateway.submit_group(_group("p-2"))
    rig.store.tamper_gap(_GRAPH, 1)
    restarted = rig.restart()
    with pytest.raises((GraphReplayError, GraphLogGapError)):
        restarted.replay_graph(_GRAPH)

    with pytest.raises(GraphReplayGatedError):
        restarted.read_log_position(_GRAPH)
    with pytest.raises(GraphReplayGatedError):
        restarted.read_persisted_elements(_GRAPH)


def test_postgres_down_raises_the_log_unavailable_error_after_the_retry_budget() -> None:
    rig = GatewayRig()
    rig.store.fail_reads()

    with pytest.raises(GraphLogUnavailableError):
        rig.gateway.read_log_position(_GRAPH)

    assert rig.sleeps  # the retry budget was spent, not a first-failure surrender
