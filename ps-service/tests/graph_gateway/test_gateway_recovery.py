"""Startup recovery of the Graph Write Gateway (issue #206, S13b; AC-BI-011, AC-BI-010).

`recover()` finds every graph whose log is ahead of its applied marker and applies the owed
entries. It never raises for a graph that is down or refuses an entry: that graph stays gated
(writes to it fail closed) until it is applied. The reconciler starts only if something is
still pending.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import GatewayRig
from ps_service.graph_gateway.errors import (
    GraphApplyBlockedError,
    GraphApplyError,
    GraphLogUnavailableError,
    GraphUnavailableError,
)
from ps_service.graph_gateway.models import MutationGroup, RecoveryResult, UpsertNode

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Protocol

    from ps_service.logging import LogEmitter

    class MakeEmitter(Protocol):
        """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

        def __call__(self) -> tuple[LogEmitter, Path]: ...

    class ReadLines(Protocol):
        """Call shape of the shared `read_lines` fixture (`tests/conftest.py`)."""

        def __call__(self, log_path: Path) -> list[dict[str, object]]: ...


_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_OTHER = "operations"
_DOWN = redis.exceptions.ConnectionError("Error 111 connecting to 10.1.2.3:6379.")
_REFUSED = redis.exceptions.ResponseError("refused")


def _group(node_id: str = "cap-1", graph: str = _GRAPH) -> MutationGroup:
    return MutationGroup(
        graph=graph,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(UpsertNode(label="Capability", id=node_id, properties={"name": node_id}),),
    )


def _lag(rig: GatewayRig, graph: str, error: Exception, node_id: str = "cap-1") -> None:
    """Commit a group whose apply fails with `error`, leaving the graph behind its log."""
    rig.graphs.open(graph).fail_on_write(error)
    with pytest.raises((GraphApplyError, GraphUnavailableError)):
        rig.gateway.submit_group(_group(node_id, graph))
    rig.gateway.stop_reconciler()


def _lag_while_down(rig: GatewayRig, graph: str, node_id: str = "cap-1") -> None:
    rig.graphs.open(graph).fail_on_write(_DOWN)
    outcome = rig.gateway.submit_group(_group(node_id, graph))
    assert outcome.status == "committed_apply_pending"


def test_new_gateway_on_log_with_unapplied_entries_applies_them_before_accepting_a_write_to_that_graph() -> (  # noqa: E501 - name mirrors PLAN S13 verbatim
    None
):
    rig = GatewayRig()
    _lag_while_down(rig, _GRAPH)
    rig.graphs.open(_GRAPH).heal()
    rig.restart()
    rig.events.clear()

    result = rig.gateway.recover()
    assert result == RecoveryResult(recovered=(_GRAPH,), gated=())
    assert rig.store.read_applied_position(_GRAPH) == 1
    assert set(rig.graphs.open(_GRAPH).nodes) == {("Capability", "cap-1")}

    assert rig.gateway.submit_group(_group("cap-2")).status == "applied"
    assert rig.events.index("graph_write") < rig.events.index("log_append")


def test_recover_applies_every_lagging_graph() -> None:
    rig = GatewayRig()
    _lag_while_down(rig, _GRAPH)
    _lag_while_down(rig, _OTHER)
    rig.graphs.open(_GRAPH).heal()
    rig.graphs.open(_OTHER).heal()
    rig.restart()

    result = rig.gateway.recover()

    assert result == RecoveryResult(recovered=(_GRAPH, _OTHER), gated=())
    assert rig.gateway.is_caught_up(_GRAPH)
    assert rig.gateway.is_caught_up(_OTHER)


def test_recover_on_a_log_with_nothing_pending_does_nothing() -> None:
    rig = GatewayRig()
    assert rig.gateway.submit_group(_group()).status == "applied"
    rig.restart()
    rig.events.clear()

    result = rig.gateway.recover()

    assert result == RecoveryResult(recovered=(), gated=())
    assert rig.events == []


def test_startup_recovery_with_graph_down_does_not_block_startup_and_leaves_graph_gated_until_applied() -> (  # noqa: E501 - name mirrors PLAN S13 verbatim
    None
):
    rig = GatewayRig()
    _lag_while_down(rig, _GRAPH)
    rig.restart()  # graph is still down

    result = rig.gateway.recover()

    assert result == RecoveryResult(recovered=(), gated=(_GRAPH,))
    assert rig.gateway.is_reconciling
    logged_before = rig.store.last_position(_GRAPH)
    with pytest.raises(GraphUnavailableError):
        rig.gateway.submit_group(_group("cap-2"))
    assert rig.store.last_position(_GRAPH) == logged_before

    rig.graphs.open(_GRAPH).heal()
    assert rig.gateway.submit_group(_group("cap-2")).status == "applied"
    assert set(rig.graphs.open(_GRAPH).nodes) == {("Capability", "cap-1"), ("Capability", "cap-2")}


def test_failed_recovery_leaves_graph_unreconciled() -> None:
    rig = GatewayRig()
    _lag_while_down(rig, _GRAPH)
    rig.restart()
    rig.gateway.recover()

    assert rig.gateway.is_caught_up(_GRAPH) is False

    rig.graphs.open(_GRAPH).heal()
    rig.gateway.submit_group(_group("cap-2"))  # re-runs the catch-up, then logs
    assert rig.gateway.is_caught_up(_GRAPH) is True


def test_recover_with_nothing_pending_does_not_start_the_reconciler() -> None:
    rig = GatewayRig()

    rig.gateway.recover()

    assert rig.gateway.is_reconciling is False


def test_recover_continues_past_a_graph_the_graph_store_refuses() -> None:
    rig = GatewayRig()
    _lag(rig, _GRAPH, _REFUSED)
    _lag_while_down(rig, _OTHER)
    rig.graphs.open(_OTHER).heal()
    rig.restart()
    rig.graphs.open(_GRAPH).fail_on_write(_REFUSED)

    result = rig.gateway.recover()

    assert result == RecoveryResult(recovered=(_OTHER,), gated=(_GRAPH,))
    with pytest.raises(GraphApplyBlockedError):
        rig.gateway.submit_group(_group("cap-2"))


def test_restart_recover_retries_blocked_graph_once() -> None:
    rig = GatewayRig()
    _lag(rig, _GRAPH, _REFUSED)
    graph = rig.graphs.open(_GRAPH)
    graph.heal()
    rig.restart()
    queries_before = len(graph.queries)

    result = rig.gateway.recover()

    assert result == RecoveryResult(recovered=(_GRAPH,), gated=())
    assert len([q for q in graph.queries[queries_before:] if q.kind == "write"]) == 1
    assert rig.gateway.submit_group(_group("cap-2")).status == "applied"


def test_restart_recover_with_a_still_refused_entry_stays_gated_and_logs_nothing_new() -> None:
    rig = GatewayRig()
    _lag(rig, _GRAPH, _REFUSED)
    rig.restart()

    result = rig.gateway.recover()

    assert result == RecoveryResult(recovered=(), gated=(_GRAPH,))
    with pytest.raises(GraphApplyBlockedError):
        rig.gateway.submit_group(_group("cap-2"))
    assert rig.store.last_position(_GRAPH) == 1
    assert rig.gateway.is_reconciling is False  # a refused entry is not retried in the background


def test_recover_raises_when_the_log_cannot_be_read() -> None:
    rig = GatewayRig()
    rig.store.fail_reads()

    with pytest.raises(GraphLogUnavailableError):
        rig.gateway.recover()


def test_recovery_is_logged_per_graph_with_the_graph_name_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = GatewayRig(emitter)
    _lag_while_down(rig, _GRAPH)
    rig.restart()

    rig.gateway.recover()
    emitter.flush()

    (entry,) = [e for e in read_lines(log_path) if e.get("action") == "startup_recovery"]
    assert entry["outcome"] == "pending"
    assert entry["graph"] == _GRAPH
    assert "error_class" not in entry
    assert "10.1.2.3" not in str(entry)
