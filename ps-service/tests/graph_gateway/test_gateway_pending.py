"""Committed, apply pending and the permanent-error path (issue #206, S11; AC-BI-010, F2).

A transient FalkorDB failure after the commit is not an error: the group is in the log, the
caller is told so, and the entries stay beyond the applied marker. A permanent failure (the
graph refuses the query) raises `GraphApplyError`, blocks the graph so no further group is
logged for it, and is not retried.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from pydantic import ValidationError

from graph_gateway._fakes import GatewayRig
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.errors import GraphApplyBlockedError, GraphApplyError
from ps_service.graph_gateway.models import GroupOutcome, MutationGroup, UpsertNode

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging import LogEmitter

    MakeEmitter = Callable[[], tuple[LogEmitter, Path]]
    ReadLines = Callable[[Path], list[dict[str, object]]]

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_BACKOFF = [0.2, 0.4, 0.8]
_HOST_TEXT = "Error 111 connecting to 10.1.2.3:6379. Connection refused."


def _group(node_id: str = "cap-1") -> MutationGroup:
    return MutationGroup(
        graph=_GRAPH,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(UpsertNode(label="Capability", id=node_id, properties={"name": node_id}),),
    )


def test_graph_unavailable_after_commit_returns_committed_apply_pending_not_an_error() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ConnectionError(_HOST_TEXT))

    outcome = rig.gateway.submit_group(_group())

    assert outcome.status == "committed_apply_pending"
    assert (outcome.graph, outcome.first_position, outcome.last_position) == (_GRAPH, 1, 1)
    assert rig.sleeps == _BACKOFF
    assert not is_healthy(FALKORDB)


def test_pending_outcome_keeps_marker_behind_and_entries_in_log() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_write(redis.exceptions.TimeoutError("slow"))

    rig.gateway.submit_group(_group())

    assert rig.store.last_position(_GRAPH) == 1
    assert rig.store.read_applied_position(_GRAPH) == 0
    assert [e.identity for e in rig.store.read_entries(_GRAPH)] == ["cap-1"]
    assert graph.nodes == {}


def test_transient_failure_after_commit_recovers_within_budget_and_marks_healthy() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ConnectionError(_HOST_TEXT), times=2)

    outcome = rig.gateway.submit_group(_group())

    assert outcome.status == "applied"
    assert rig.sleeps == [0.2, 0.4]
    assert is_healthy(FALKORDB)


def test_apply_failure_is_logged_with_graph_and_sequence(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ConnectionError(_HOST_TEXT))

    rig.gateway.submit_group(_group())
    emitter.flush()

    (line,) = [e for e in read_lines(log_path) if e.get("action") == "apply_group"]
    assert (line["component"], line["outcome"]) == ("graph_gateway", "pending")
    assert (line["graph"], line["first_position"], line["last_position"]) == (_GRAPH, 1, 1)
    assert line["error_class"] == "ConnectionError"
    assert "10.1.2.3" not in json.dumps(line)
    assert "cap-1" not in json.dumps(line)


def test_permanent_apply_error_after_commit_raises_and_blocks_the_graph() -> None:
    rig = GatewayRig()
    cause = redis.exceptions.ResponseError("Invalid input near secret-token")
    rig.graphs.open(_GRAPH).fail_on_write(cause)

    with pytest.raises(GraphApplyError) as raised:
        rig.gateway.submit_group(_group())

    assert (raised.value.graph, raised.value.position) == (_GRAPH, 1)
    assert "secret-token" not in str(raised.value)
    assert raised.value.__cause__ is cause
    assert rig.sleeps == []
    assert rig.store.last_position(_GRAPH) == 1
    assert rig.store.read_applied_position(_GRAPH) == 0
    assert is_healthy(FALKORDB)


def test_a_refused_index_creation_is_a_permanent_apply_error() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_index(redis.exceptions.ResponseError("boom"))

    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group())

    assert rig.sleeps == []
    assert rig.store.read_applied_position(_GRAPH) == 0


def test_blocked_graph_rejects_new_group_and_logs_nothing() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_write(redis.exceptions.ResponseError("refused"))
    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group())
    graph.heal()
    graph.queries.clear()

    with pytest.raises(GraphApplyBlockedError) as raised:
        rig.gateway.submit_group(_group("cap-2"))

    assert raised.value.graph == _GRAPH
    assert isinstance(raised.value, GraphApplyError)
    assert rig.store.last_position(_GRAPH) == 1
    assert graph.queries == []


def test_a_blocked_graph_does_not_block_other_graphs() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ResponseError("refused"))
    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group())

    other = MutationGroup(
        graph="other", audit_event_id=_AUDIT_EVENT_ID, primitives=_group().primitives
    )

    assert rig.gateway.submit_group(other).status == "applied"


def test_restart_retries_blocked_graph_once_and_applies_when_healed() -> None:
    rig = GatewayRig()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_write(redis.exceptions.ResponseError("refused"))
    with pytest.raises(GraphApplyError):
        rig.gateway.submit_group(_group())
    graph.heal()
    rig.restart()

    outcome = rig.gateway.submit_group(_group("cap-2"))

    assert outcome.status == "applied"
    assert {node_id for _, node_id in graph.nodes} == {"cap-1", "cap-2"}
    assert rig.store.read_applied_position(_GRAPH) == 2


def test_in_transaction_complete_reports_pending_when_the_graph_is_down() -> None:
    rig = GatewayRig()
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ConnectionError(_HOST_TEXT))
    transaction = rig.store.begin()
    audit_event_id = transaction.record_audit()
    group = MutationGroup(
        graph=_GRAPH, audit_event_id=audit_event_id, primitives=_group().primitives
    )

    with rig.gateway.submit_group_in_transaction(transaction.cursor, group) as staged:
        transaction.commit()
        outcome = staged.complete()

    assert outcome.status == "committed_apply_pending"
    assert rig.store.read_applied_position(_GRAPH) == 0


def test_a_group_outcome_pending_carries_positions_like_an_applied_one() -> None:
    pending = GroupOutcome(
        graph=_GRAPH, first_position=1, last_position=2, status="committed_apply_pending"
    )

    assert (pending.first_position, pending.last_position) == (1, 2)
    with pytest.raises(ValidationError):
        GroupOutcome(
            graph=_GRAPH, first_position=None, last_position=None, status="committed_apply_pending"
        )
