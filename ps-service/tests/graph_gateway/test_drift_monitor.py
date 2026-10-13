"""The periodic drift monitor: one pass per interval over every logged graph (issue #215, S5b).

AC-BI-007: the interval is configurable and `0` turns the monitor off. The thread is a perpetual
daemon modelled on the reconciler's mechanics (interruptible wait, injectable pause, `stop`); a
failing pass never ends it. Passes run on the gateway's own `check_drift`, so what a check finds is
covered by `test_drift_check.py`; here only the schedule and the pass log are pinned.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import Fault, GatewayRig, SteppedWait
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.logging import EmitterConfig, LogEmitter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_AUDIT = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPHS = ("alpha", "beta")


class _Logs:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "monitor.jsonl"
        self.emitter = LogEmitter(EmitterConfig(log_path=self.path, fallback=StringIO()))

    def entries(self, action: str) -> list[dict[str, object]]:
        self.emitter.flush()
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        return [e for e in (json.loads(line) for line in lines if line) if e["action"] == action]


@pytest.fixture
def logs(tmp_path: Path) -> _Logs:
    return _Logs(tmp_path)


def _seed(rig: GatewayRig) -> None:
    for graph in _GRAPHS:
        rig.gateway.submit_group(
            MutationGroup(
                graph=graph,
                audit_event_id=_AUDIT,
                primitives=(UpsertNode(label="Capability", id=f"{graph}-1", properties={}),),
                checkpoint_requested=True,
            )
        )


def _gateway(rig: GatewayRig, logs: _Logs, wait: SteppedWait | None = None) -> GraphWriteGateway:
    return GraphWriteGateway(
        log_store=rig.store,
        graph_opener=rig.graphs.open,
        emitter=logs.emitter,
        reconciler_wait=lambda _delay: True,
        drift_wait=wait,
    )


@pytest.fixture
def rig() -> Iterator[GatewayRig]:
    gateway_rig = GatewayRig()
    _seed(gateway_rig)
    yield gateway_rig
    gateway_rig.close()


def test_the_monitor_runs_a_pass_per_interval_over_every_logged_graph(
    rig: GatewayRig, logs: _Logs
) -> None:
    wait = SteppedWait()
    gateway = _gateway(rig, logs, wait)

    assert gateway.start_drift_monitor(60) is True
    try:
        wait.await_wait()  # the first pass comes one interval after the start, not at startup
        assert logs.entries("drift_check") == []
        wait.resume()
        wait.await_wait()  # parked again: the pass has finished
        wait.resume()
        wait.await_wait()
    finally:
        wait.stopped.set()
        wait.resume()
        assert gateway.stop_drift_monitor(5.0)

    assert wait.delays[:3] == [60, 60, 60]
    checks = logs.entries("drift_check")
    assert sorted(str(c["graph"]) for c in checks) == sorted(_GRAPHS * 2)
    assert {c["outcome"] for c in checks} == {"ok"}
    passes = logs.entries("drift_pass")
    assert [(p["outcome"], p["graph_count"], p["drift_count"]) for p in passes] == [
        ("completed", 2, 0)
    ] * 2
    assert all(isinstance(p["duration_ms"], float) for p in passes)


def test_a_pass_counts_the_graphs_that_drifted(rig: GatewayRig, logs: _Logs) -> None:
    rig.graphs.open("alpha").seed_node(("Capability",), "out-of-band", {})
    gateway = _gateway(rig, logs)

    result = gateway.check_all_drift()

    assert result.drift_count == 1
    [passed] = logs.entries("drift_pass")
    assert (passed["graph_count"], passed["drift_count"]) == (2, 1)


def test_stop_interrupts_the_wait_and_ends_the_thread(rig: GatewayRig, logs: _Logs) -> None:
    gateway = _gateway(rig, logs)  # the real, interruptible wait

    assert gateway.start_drift_monitor(3600) is True
    stopped = gateway.stop_drift_monitor(5.0)

    assert stopped is True
    assert [e["outcome"] for e in logs.entries("drift_monitor")] == ["started", "stopped"]
    assert logs.entries("drift_check") == []
    assert {e["interval_seconds"] for e in logs.entries("drift_monitor")} == {3600}


def test_a_pass_never_raises_into_the_thread(rig: GatewayRig, logs: _Logs) -> None:
    wait = SteppedWait()
    gateway = _gateway(rig, logs, wait)
    rig.store.read_fault = Fault(redis.exceptions.ConnectionError("pg down"), times=1)

    gateway.start_drift_monitor(60)
    try:
        wait.await_wait()
        wait.resume()  # the first pass cannot list the graphs
        wait.await_wait()
        wait.resume()  # the second pass works: the thread survived
        wait.await_wait()
    finally:
        wait.stopped.set()
        wait.resume()
        gateway.stop_drift_monitor(5.0)

    outcomes = [str(p["outcome"]) for p in logs.entries("drift_pass")]
    assert outcomes == ["failed", "completed"]
    [failed, _completed] = logs.entries("drift_pass")
    assert failed["error_class"] == "ConnectionError"


def test_interval_zero_starts_no_thread(rig: GatewayRig, logs: _Logs) -> None:
    gateway = _gateway(rig, logs)

    assert gateway.start_drift_monitor(0) is False

    assert gateway.stop_drift_monitor(1.0) is True
    [entry] = logs.entries("drift_monitor")
    assert (entry["outcome"], entry["interval_seconds"]) == ("disabled", 0)
    assert logs.entries("drift_check") == []
