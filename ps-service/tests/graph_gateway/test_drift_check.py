"""`check_drift`: compare a graph with its head checkpoint, alert, never repair (issue #215, S5a).

AC-BI-005: a graph that changed outside the gateway since the checkpoint at its head raises an
alert (outcome `failed`, `error_class` `GraphDriftDetected`, naming graph, position and both
digests, D-6); a graph without a checkpoint at its head is logged `no_baseline`.
AC-BI-006: a graph with unapplied entries, a write in flight, a held lock or a closed gate is
skipped without an alert and without waiting. AC-BI-007: an unreachable FalkorDB is a logged
failure that leaves the dependency health alone.
"""

from __future__ import annotations

import json
import threading
from io import StringIO
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions

from graph_gateway._fakes import Fault, GatewayRig, InMemoryGraph
from ps_service.dependency_health import FALKORDB, is_healthy, mark_healthy
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.gateway_log import ALLOWED_LOG_FIELDS, emit_gateway_event
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.graph_gateway.read_only import open_read_only_database
from ps_service.logging import EmitterConfig, LogEmitter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from ps_service.graph_gateway.connection import GraphQueryResult
    from ps_service.graph_gateway.read_only import DriverGraph, ReadOnlyGraph, ReadOnlyQueryResult

_AUDIT = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "compliance"
_SECRET = "s3cr3t-property-value"


class _Logs:
    """An emitter that mirrors failures to a captured stderr sink, plus the file it writes."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "drift.jsonl"
        self.stderr = StringIO()
        self.emitter = LogEmitter(
            EmitterConfig(log_path=self.path, fallback=self.stderr, mirror_failures_to_stderr=True)
        )

    def drift_entries(self) -> list[dict[str, object]]:
        self.emitter.flush()
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        entries = [json.loads(line) for line in lines if line]
        return [e for e in entries if e["action"] == "drift_check"]


@pytest.fixture
def logs(tmp_path: Path) -> _Logs:
    return _Logs(tmp_path)


def _node(node_id: str) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": _SECRET})


def _submit(rig: GatewayRig, node_id: str, *, checkpoint: bool = False) -> None:
    rig.gateway.submit_group(
        MutationGroup(
            graph=_GRAPH,
            audit_event_id=_AUDIT,
            primitives=(_node(node_id),),
            checkpoint_requested=checkpoint,
        )
    )


def _write_out_of_band(rig: GatewayRig) -> None:
    """A write the gateway never saw: straight into the graph, no log entry."""
    rig.graphs.open(_GRAPH).seed_node(("Capability",), "out-of-band", {"name": "x"})


def _rig(logs: _Logs) -> GatewayRig:
    return GatewayRig(logs.emitter)


def _checking_gateway(
    rig: GatewayRig, read_graph_opener: Callable[[str], ReadOnlyGraph]
) -> GraphWriteGateway:
    """A second gateway over the rig's log and graphs that reads drift through `opener`."""
    return GraphWriteGateway(
        log_store=rig.store,
        graph_opener=rig.graphs.open,
        emitter=rig.gateway._emitter,  # pyright: ignore[reportPrivateUsage]
        read_graph_opener=read_graph_opener,
        reconciler_wait=lambda _delay: True,
    )


def test_a_graph_whose_live_digest_matches_the_head_checkpoint_is_ok(logs: _Logs) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)

    result = rig.gateway.check_drift(_GRAPH)

    assert result.status == "ok"
    [entry] = logs.drift_entries()
    assert (entry["outcome"], entry["graph"], entry["last_position"]) == ("ok", _GRAPH, 1)
    assert logs.stderr.getvalue() == ""


def test_an_out_of_band_write_after_a_checkpoint_at_the_head_raises_an_alert_naming_graph_position_and_both_digests(  # noqa: E501 -- name fixed by the plan
    logs: _Logs,
) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)
    stored = rig.store.read_digest_checkpoint(_GRAPH, 1)
    assert stored is not None
    _write_out_of_band(rig)

    result = rig.gateway.check_drift(_GRAPH)

    live = canonical_digest(rig.graphs.open(_GRAPH))
    assert result.status == "drift"
    assert live != stored.canonical_digest
    [entry] = logs.drift_entries()
    assert entry["outcome"] == "failed"
    assert entry["error_class"] == "GraphDriftDetected"
    assert (entry["graph"], entry["last_position"]) == (_GRAPH, 1)
    assert (entry["checkpoint_digest"], entry["live_digest"]) == (stored.canonical_digest, live)
    mirrored = json.loads(logs.stderr.getvalue().strip())
    assert (mirrored["action"], mirrored["error_class"]) == ("drift_check", "GraphDriftDetected")


def test_a_graph_without_a_checkpoint_at_the_head_is_logged_no_baseline_and_skipped(
    logs: _Logs,
) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)
    _submit(rig, "cap-2")  # the head moved past the checkpoint
    _write_out_of_band(rig)

    result = rig.gateway.check_drift(_GRAPH)

    assert result.status == "no_baseline"
    [entry] = logs.drift_entries()
    assert (entry["outcome"], entry["graph"], entry["last_position"]) == ("no_baseline", _GRAPH, 2)
    assert logs.stderr.getvalue() == ""


def test_unapplied_entries_raise_no_alert(logs: _Logs) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ConnectionError("down"))
    _submit(rig, "cap-2")  # committed, apply pending: the marker stays behind the log
    rig.graphs.open(_GRAPH).heal()
    _write_out_of_band(rig)

    result = rig.gateway.check_drift(_GRAPH)

    assert (result.status, result.reason) == ("skipped", "unapplied")
    [entry] = logs.drift_entries()
    assert (entry["outcome"], entry["reason"]) == ("skipped", "unapplied")
    assert logs.stderr.getvalue() == ""


def test_a_write_in_flight_raises_no_alert(logs: _Logs) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)
    fired: list[bool] = []

    def opener(name: str) -> ReadOnlyGraph:
        graph = rig.graphs.open(name)

        class _Racing:
            def query(
                self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
            ) -> GraphQueryResult:
                del timeout
                if not fired:
                    fired.append(True)
                    _submit(rig, "cap-2")  # a gateway write lands while the digest is read
                return graph.query(q, params)

        return cast("ReadOnlyGraph", _Racing())

    result = _checking_gateway(rig, opener).check_drift(_GRAPH)

    assert (result.status, result.reason) == ("skipped", "in_flight")
    assert [e["outcome"] for e in logs.drift_entries()] == ["skipped"]
    assert logs.stderr.getvalue() == ""


def test_a_graph_whose_lock_is_held_is_skipped_without_waiting(logs: _Logs) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)
    graph = rig.graphs.open(_GRAPH)
    inside, release = threading.Event(), threading.Event()

    def block() -> None:
        graph.on_write = None
        inside.set()
        assert release.wait(timeout=5.0)

    graph.on_write = block
    writer = threading.Thread(target=_submit, args=(rig, "cap-2"))
    writer.start()
    try:
        assert inside.wait(timeout=5.0), "the writer never reached its apply"
        results: list[str] = []
        checker = threading.Thread(
            target=lambda: results.append(rig.gateway.check_drift(_GRAPH).status)
        )
        checker.start()
        checker.join(timeout=2.0)
        assert not checker.is_alive(), "the drift check waited for the graph lock"
        assert results == ["skipped"]
    finally:
        release.set()
        writer.join(timeout=5.0)
    [entry] = logs.drift_entries()
    assert (entry["outcome"], entry["reason"]) == ("skipped", "busy")
    assert logs.stderr.getvalue() == ""


def test_a_gated_graph_is_skipped(logs: _Logs) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)
    rig.gateway.hold_for_startup_replay()

    result = rig.gateway.check_drift(_GRAPH)

    assert (result.status, result.reason) == ("skipped", "gated")
    [entry] = logs.drift_entries()
    assert (entry["outcome"], entry["reason"]) == ("skipped", "gated")


def test_unreachable_falkordb_logs_failed_and_leaves_dependency_health_untouched(
    logs: _Logs,
) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)

    class _Down:
        def query(
            self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
        ) -> GraphQueryResult:
            del q, params, timeout
            raise redis.exceptions.ConnectionError("Error 111 connecting to 10.1.2.3:6379")

    mark_healthy(FALKORDB)
    before = is_healthy(FALKORDB)

    result = _checking_gateway(rig, lambda _name: cast("ReadOnlyGraph", _Down())).check_drift(
        _GRAPH
    )

    assert result.status == "failed"
    assert is_healthy(FALKORDB) == before
    [entry] = logs.drift_entries()
    assert (entry["outcome"], entry["error_class"]) == ("failed", "ConnectionError")
    assert "10.1.2.3" not in json.dumps(entry)


def test_a_graph_log_that_cannot_be_read_is_a_logged_failure_not_an_exception(
    logs: _Logs,
) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)
    rig.store.read_fault = Fault(redis.exceptions.ConnectionError("pg down"))

    result = rig.gateway.check_drift(_GRAPH)

    assert result.status == "failed"
    [entry] = logs.drift_entries()
    assert entry["outcome"] == "failed"


def test_logging_names_digests_and_positions_only(logs: _Logs) -> None:
    rig = _rig(logs)
    _submit(rig, "secret-node-id-77", checkpoint=True)
    _write_out_of_band(rig)
    rig.gateway.check_drift(_GRAPH)

    [entry] = logs.drift_entries()
    allowed = ALLOWED_LOG_FIELDS | {"component", "action", "outcome", "timestamp"}
    assert set(entry) <= allowed | {"run_id", "duration_ms"}
    text = json.dumps(entry)
    for leak in (_SECRET, "secret-node-id-77", "Capability", "out-of-band"):
        assert leak not in text


def test_the_gateway_log_accepts_the_drift_fields_and_still_refuses_a_label() -> None:
    fields: dict[str, str | int | float] = {
        "graph": _GRAPH,
        "checkpoint_digest": "sha256:a",
        "live_digest": "sha256:b",
        "reason": "busy",
        "graph_count": 1,
        "drift_count": 0,
        "interval_seconds": 3600,
    }

    emit_gateway_event("drift_check", "ok", fields, emitter=None)
    with pytest.raises(ValueError, match="not allowed"):
        emit_gateway_event("drift_check", "ok", {"label": "Capability"}, emitter=None)


class _RecordingDriverGraph:
    """A driver graph that records which method each query used; only `ro_query` is allowed."""

    def __init__(self, inner: InMemoryGraph, calls: list[str]) -> None:
        self._inner = inner
        self._calls = calls

    def ro_query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> ReadOnlyQueryResult:
        del timeout
        self._calls.append("ro_query")
        return cast("ReadOnlyQueryResult", self._inner.query(q, params))

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        del q, params
        self._calls.append("query")
        message = "the drift check used GRAPH.QUERY"
        raise AssertionError(message)


class _RecordingDriverDatabase:
    def __init__(self, rig: GatewayRig, calls: list[str]) -> None:
        self._rig = rig
        self._calls = calls

    def select_graph(self, graph_id: str, /) -> DriverGraph:
        return _RecordingDriverGraph(self._rig.graphs.open(graph_id), self._calls)

    def list_graphs(self) -> list[str]:
        return list(self._rig.store.logged_graphs())


def test_drift_digest_is_read_with_ro_query_only(logs: _Logs) -> None:
    rig = _rig(logs)
    _submit(rig, "cap-1", checkpoint=True)
    calls: list[str] = []
    database = open_read_only_database(
        "h", 1, connect=lambda _h, _p: _RecordingDriverDatabase(rig, calls)
    )

    result = _checking_gateway(rig, database.select_graph).check_drift(_GRAPH)

    assert result.status == "ok"
    assert calls
    assert set(calls) == {"ro_query"}


@pytest.fixture(autouse=True)
def _healthy() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    mark_healthy(FALKORDB)
    yield
    mark_healthy(FALKORDB)
