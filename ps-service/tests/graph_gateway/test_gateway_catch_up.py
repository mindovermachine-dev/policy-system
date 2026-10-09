"""`catch_up`, `is_caught_up` and submit-time catch-up (issue #206, S12; AC-BI-010, AC-BI-012).

`catch_up(graph)` applies every logged entry beyond the applied marker, in order. `is_caught_up`
is a read-only predicate that answers True only when the marker equals the last logged position
and raises, rather than answering, when the log cannot be read. A write to a lagging graph
catches the graph up before it validates or logs anything.
"""

from __future__ import annotations

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.entry_codec import encode_primitive
from ps_service.graph_gateway.errors import (
    GraphApplyBlockedError,
    GraphApplyError,
    GraphLogUnavailableError,
    GraphUnavailableError,
)
from ps_service.graph_gateway.models import (
    DeleteNode,
    GraphLogGroupDraft,
    MutationGroup,
    Primitive,
    UpsertNode,
)

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_DOWN = redis.exceptions.ConnectionError("Error 111 connecting to 10.1.2.3:6379.")


def _node(node_id: str, **properties: object) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties=properties)


def _group(*primitives: Primitive) -> MutationGroup:
    return MutationGroup(graph=_GRAPH, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)


def _rig_with_pending_entries(*primitives: Primitive) -> GatewayRig:
    """A rig whose graph was down for the first group: committed, apply pending."""
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(_DOWN)
    outcome = rig.gateway.submit_group(_group(*primitives))
    assert outcome.status == "committed_apply_pending"
    return rig


def test_catch_up_applies_pending_entries_in_order_and_advances_marker() -> None:
    rig = _rig_with_pending_entries(
        _node("x", version=1), DeleteNode(label="Capability", id="x"), _node("x", version=3)
    )
    graph = rig.graphs.open(_GRAPH)
    graph.heal()

    result = rig.gateway.catch_up(_GRAPH)

    assert (result.graph, result.applied_position, result.last_position) == (_GRAPH, 3, 3)
    assert result.caught_up is True
    assert graph.nodes == {("Capability", "x"): {"version": 3}}
    assert [q.template for q in graph.queries if q.kind == "write"] == [
        "upsert_node",
        "delete_node",
        "upsert_node",
    ]
    assert rig.store.read_applied_position(_GRAPH) == 3
    assert is_healthy(FALKORDB)


def test_catch_up_twice_applies_each_entry_once() -> None:
    rig = _rig_with_pending_entries(_node("x"))
    graph = rig.graphs.open(_GRAPH)
    graph.heal()
    rig.gateway.catch_up(_GRAPH)
    graph.queries.clear()

    again = rig.gateway.catch_up(_GRAPH)

    assert again.caught_up is True
    assert graph.queries == []


def test_catch_up_reports_not_caught_up_while_the_graph_stays_down() -> None:
    rig = _rig_with_pending_entries(_node("x"))

    result = rig.gateway.catch_up(_GRAPH)

    assert (result.applied_position, result.last_position, result.caught_up) == (0, 1, False)
    assert rig.store.last_position(_GRAPH) == 1


def test_catch_up_on_a_graph_with_nothing_logged_is_caught_up() -> None:
    rig = GatewayRig()

    result = rig.gateway.catch_up(_GRAPH)

    assert (result.applied_position, result.last_position, result.caught_up) == (0, 0, True)


def test_is_caught_up_true_only_when_marker_equals_last_position() -> None:
    rig = GatewayRig()
    assert rig.gateway.is_caught_up(_GRAPH) is True  # nothing logged, nothing to apply

    rig.gateway.submit_group(_group(_node("x")))
    assert rig.gateway.is_caught_up(_GRAPH) is True

    rig.store.append_group_standalone(
        GraphLogGroupDraft(graph=_GRAPH, entries=(encode_primitive(_node("y")),)),
        audit_event_id=_AUDIT_EVENT_ID,
    )  # committed behind the gateway's back
    assert rig.gateway.is_caught_up(_GRAPH) is False
    rig.gateway.catch_up(_GRAPH)
    assert rig.gateway.is_caught_up(_GRAPH) is True


def test_is_caught_up_false_after_pending_outcome() -> None:
    rig = _rig_with_pending_entries(_node("x"))

    assert rig.gateway.is_caught_up(_GRAPH) is False

    rig.graphs.open(_GRAPH).heal()
    rig.gateway.catch_up(_GRAPH)
    assert rig.gateway.is_caught_up(_GRAPH) is True


def test_is_caught_up_raises_rather_than_returning_true_when_log_unreadable() -> None:
    rig = GatewayRig()  # marker 0 == last position 0: True if the log were not unreadable
    rig.store.fail_reads()

    with pytest.raises(GraphLogUnavailableError):
        rig.gateway.is_caught_up(_GRAPH)


def test_submit_on_lagging_graph_catches_up_before_new_group() -> None:
    rig = _rig_with_pending_entries(_node("first"))
    graph = rig.graphs.open(_GRAPH)
    graph.heal()
    before = len(rig.events)

    outcome = rig.gateway.submit_group(_group(_node("second")))

    assert outcome.status == "applied"
    assert (outcome.first_position, outcome.last_position) == (2, 2)
    assert rig.events[before:] == ["graph_write", "graph_read", "log_append", "graph_write"]
    assert graph.upsert_order == [("Capability", "first"), ("Capability", "second")]
    assert rig.store.read_applied_position(_GRAPH) == 2


def test_write_to_lagging_graph_with_graph_down_raises_and_logs_nothing() -> None:
    rig = _rig_with_pending_entries(_node("first"))

    with pytest.raises(GraphUnavailableError):
        rig.gateway.submit_group(_group(_node("second")))

    assert rig.store.last_position(_GRAPH) == 1
    assert rig.store.read_applied_position(_GRAPH) == 0
    assert rig.store.append_attempts == 1  # the first group only


def test_a_successful_catch_up_unblocks_a_graph_the_graph_had_refused() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_write(redis.exceptions.ResponseError("refused"))
    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group(_node("x")))
    with pytest.raises(GraphApplyBlockedError):
        rig.gateway.submit_group(_group(_node("y")))
    graph.heal()

    result = rig.gateway.catch_up(_GRAPH)

    assert result.caught_up is True
    assert rig.gateway.submit_group(_group(_node("y"))).status == "applied"


def test_catch_up_that_the_graph_still_refuses_raises_and_keeps_the_graph_blocked() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ResponseError("refused"))
    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group(_node("x")))

    with pytest.raises(GraphApplyError) as raised:
        rig.gateway.catch_up(_GRAPH)

    assert raised.value.position == 1
    with pytest.raises(GraphApplyBlockedError):
        rig.gateway.submit_group(_group(_node("y")))
    assert rig.store.last_position(_GRAPH) == 1


def test_restart_with_a_still_refused_entry_fails_before_anything_new_is_logged() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ResponseError("refused"))
    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group(_node("x")))
    rig.restart()

    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group(_node("y")))

    assert rig.store.last_position(_GRAPH) == 1
