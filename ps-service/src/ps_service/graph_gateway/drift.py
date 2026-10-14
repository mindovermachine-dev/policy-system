"""Drift detection: does a graph still hold what the gateway last checkpointed?

A graph the gateway wrote should only ever change through the gateway. A writer that asked for a
digest checkpoint left, at the log position of its last group, the canonical digest of the graph
as the gateway left it. `DriftChecker.check` compares that digest with the live graph and, if they
differ, logs an alert. It never repairs anything: drift is a fact for an operator to investigate.

A check is only meaningful when nothing the gateway does can be mid-way through, so it is skipped
(never alerted, never waited for) when:

- the graph is closed to writes by a replay or blocked by a permanent apply failure (`gated`);
- another thread holds the graph's lock, so a write is being applied (`busy`);
- the log holds entries the graph has not applied yet (`unapplied`);
- the head of the log or the applied marker moved while the live digest was being read
  (`in_flight`): every gateway write appends to the log before it touches the graph, so an
  unchanged head around the read proves no gateway write overlapped it.

A graph with no checkpoint at exactly its head position has no baseline to compare with
(`no_baseline`, logged). The live graph is read through a read-only handle with the lock
released, so a whole-graph digest never stalls a writer and never writes.

Only identifiers, positions, digests (hashes), counts and error classes are logged.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ps_service.graph_gateway.digest import DigestSettings, QueryableGraph, canonical_digest
from ps_service.graph_gateway.gateway_log import emit_gateway_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ps_service.graph_gateway.graph_locks import GraphLockRegistry
    from ps_service.graph_gateway.store import GraphLogStore
    from ps_service.logging import LogEmitter

__all__ = [
    "DRIFT_ERROR_CLASS",
    "DriftChecker",
    "DriftPass",
    "DriftResult",
    "DriftStatus",
    "GraphDriftMonitor",
    "SkipReason",
]

DRIFT_ERROR_CLASS = "GraphDriftDetected"
"""The `error_class` of the alert entry: outcome `failed`, so the stderr mirror shows it."""
DRIFT_MONITOR_THREAD_NAME = "graph-gateway-drift-monitor"
DEFAULT_STOP_TIMEOUT_SECONDS = 5.0

type DriftStatus = Literal["ok", "drift", "no_baseline", "skipped", "failed"]
type SkipReason = Literal["busy", "unapplied", "in_flight", "gated"]


@dataclass(frozen=True, slots=True)
class DriftResult:
    """What one check of one graph found.

    Attributes:
        graph: Name of the checked graph.
        status: Outcome: `ok`, `drift`, `no_baseline`, `skipped` or `failed`.
        reason: Why the check was skipped; set only when `status == "skipped"`.
        last_position: Log head position the check was made at; `None` when not read.
        checkpoint_digest: The recorded digest at the head; set when a comparison was made.
        live_digest: The digest of the live graph; set when a comparison was made.
        error_class: The exception class that stopped the check; set only when
            `status == "failed"`.
    """

    graph: str
    status: DriftStatus
    reason: SkipReason | None = None
    """Why the check was skipped (`status == "skipped"`)."""
    last_position: int | None = None
    checkpoint_digest: str | None = None
    live_digest: str | None = None
    error_class: str | None = None
    """The exception class that stopped the check (`status == "failed"`)."""


@dataclass(frozen=True, slots=True)
class DriftPass:
    """What one pass over every logged graph found.

    Attributes:
        results: One result per checked graph, in check order; empty when no graph was checked.
    """

    results: tuple[DriftResult, ...]

    @property
    def drift_count(self) -> int:
        """Count the graphs whose live content differs from their head checkpoint.

        Returns:
            The number of results with status `drift`; 0 when none drifted.
        """
        return sum(1 for result in self.results if result.status == "drift")


class _Baseline:
    """The head position and checkpoint digest read under the graph's lock."""

    __slots__ = ("applied", "checkpoint_digest", "last")

    def __init__(self, last: int, applied: int, checkpoint_digest: str | None) -> None:
        """Hold the head position, the applied marker and the head checkpoint digest, if any."""
        self.last = last
        self.applied = applied
        self.checkpoint_digest = checkpoint_digest


class DriftChecker:
    """Compares graphs with their head checkpoints; logs, never repairs.

    Business Rules:
        A check is skipped, never alerted, when a replay or permanent failure gates the graph,
        a writer holds its lock, the log has unapplied entries, or the head or applied marker
        moved while the live digest was read.
    """

    def __init__(
        self,
        *,
        log_store: GraphLogStore,
        read_graph_opener: Callable[[str], QueryableGraph],
        locks: GraphLockRegistry,
        is_gated: Callable[[str], bool],
        emitter: LogEmitter | None,
        digest_settings: DigestSettings | None = None,
    ) -> None:
        """Take the log, the read-only opener, the gateway's locks and gate.

        Args:
            log_store: Source of log head, applied marker and digest checkpoints.
            read_graph_opener: Opens a read-only handle on a named graph.
            locks: The gateway's per-graph locks, shared with the writers.
            is_gated: Whether a graph is closed to writes by a replay or permanent failure.
            emitter: Destination of the drift log entries; `None` disables logging.
            digest_settings: Scan chunk sizes; `None` uses the defaults.
        """
        self._log_store = log_store
        self._read_graph_opener = read_graph_opener
        self._locks = locks
        self._is_gated = is_gated
        self._emitter = emitter
        self._digest_settings = digest_settings if digest_settings is not None else DigestSettings()

    def check(self, graph: str) -> DriftResult:
        """Check `graph` once and log the finding; never raises, never waits for a writer.

        Args:
            graph: Name of the graph to check.

        Returns:
            The finding. An unexpected error is returned as status `failed` with its
            `error_class`, not raised.

        Side Effects:
            Reads the log and the live graph, and emits one `drift_check` log entry.
        """
        try:
            result = self._check(graph)
        except Exception as exc:  # noqa: BLE001  # a check must not fail its caller; it is logged
            result = DriftResult(graph=graph, status="failed", error_class=type(exc).__name__)
        self._log(result)
        return result

    def run_pass(self, graphs: Iterable[str]) -> DriftPass:
        """Check every graph in `graphs` once and log the pass (graphs checked, drifts found).

        Args:
            graphs: Names of the graphs to check; empty checks none.

        Returns:
            One result per graph, in the order given.

        Side Effects:
            Emits one `drift_check` entry per graph and one `drift_pass` entry.
        """
        started = time.monotonic()
        names = tuple(graphs)
        outcome = DriftPass(results=tuple(self.check(graph) for graph in names))
        fields: dict[str, str | int | float] = {
            "graph_count": len(names),
            "drift_count": outcome.drift_count,
        }
        emit_gateway_event(
            "drift_pass",
            "completed",
            fields,
            emitter=self._emitter,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        return outcome

    def _check(self, graph: str) -> DriftResult:
        """Check one graph, or say why the check was skipped."""
        if self._is_gated(graph):
            return _skipped(graph, "gated")
        baseline = self._read_baseline(graph)
        if baseline is None:
            return _skipped(graph, "busy")
        if baseline.applied != baseline.last:
            return _skipped(graph, "unapplied", baseline.last)
        if baseline.checkpoint_digest is None:
            return DriftResult(graph=graph, status="no_baseline", last_position=baseline.last)
        return self._compare(graph, baseline, baseline.checkpoint_digest)

    def _compare(self, graph: str, baseline: _Baseline, checkpoint_digest: str) -> DriftResult:
        """Read the live digest and compare it, unless a write could have overlapped the read."""
        live = canonical_digest(self._read_graph_opener(graph), self._digest_settings)
        if self._is_gated(graph):
            return _skipped(graph, "gated", baseline.last)
        if (
            self._log_store.last_position(graph) != baseline.last
            or self._log_store.read_applied_position(graph) != baseline.applied
        ):
            return _skipped(graph, "in_flight", baseline.last)
        return DriftResult(
            graph=graph,
            status="ok" if live == checkpoint_digest else "drift",
            last_position=baseline.last,
            checkpoint_digest=checkpoint_digest,
            live_digest=live,
        )

    def _read_baseline(self, graph: str) -> _Baseline | None:
        """Read the head, the marker and the head checkpoint, or `None` if the lock is held.

        The lock is tried, never waited for, and released before the (long) live read.
        """
        lock = self._locks.lock_for(graph)
        if not lock.acquire(blocking=False):
            return None
        try:
            last = self._log_store.last_position(graph)
            applied = self._log_store.read_applied_position(graph)
            if applied != last:
                return _Baseline(last, applied, None)
            checkpoint = self._log_store.read_digest_checkpoint(graph, last)
            return _Baseline(
                last, applied, None if checkpoint is None else checkpoint.canonical_digest
            )
        finally:
            lock.release()

    def _log(self, result: DriftResult) -> None:
        """Emit the `drift_check` entry for `result`; a drift is logged as a failed outcome."""
        fields: dict[str, str | int | float] = {"graph": result.graph}
        if result.last_position is not None:
            fields["last_position"] = result.last_position
        match result.status:
            case "ok" | "no_baseline":
                outcome = result.status
            case "skipped":
                outcome = "skipped"
                fields["reason"] = result.reason or ""
            case "drift":
                outcome = "failed"
                fields["error_class"] = DRIFT_ERROR_CLASS
                fields["checkpoint_digest"] = result.checkpoint_digest or ""
                fields["live_digest"] = result.live_digest or ""
            case "failed":
                outcome = "failed"
                fields["error_class"] = result.error_class or "DriftCheckFailed"
        emit_gateway_event("drift_check", outcome, fields, emitter=self._emitter)


def _skipped(graph: str, reason: SkipReason, last_position: int | None = None) -> DriftResult:
    """Build the result of a check that was skipped for `reason`."""
    return DriftResult(graph=graph, status="skipped", reason=reason, last_position=last_position)


class GraphDriftMonitor:
    """A perpetual daemon thread that runs one drift pass per interval.

    Thread mechanics follow `GraphReconciler` (daemon, interruptible `Event.wait`, injectable
    `wait`, `stop(timeout)`); the monitor never retires: it ends only on `stop()`. The first pass
    runs one interval after `start()`, never at startup.
    """

    def __init__(
        self,
        run_pass: Callable[[], DriftPass],
        *,
        interval_seconds: float,
        emitter: LogEmitter | None,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        """Take the pass to run and the pause between passes.

        Args:
            run_pass: Runs one drift pass over every logged graph.
            interval_seconds: Pause between passes; 0 or less disables the monitor.
            emitter: Destination of the monitor log entries; `None` disables logging.
            wait: `wait(seconds)` sleeps between passes and returns True if the monitor must
                stop. Defaults to `Event.wait` on the stop event, so `stop()` interrupts it.
        """
        self._run_pass = run_pass
        self._interval = interval_seconds
        self._emitter = emitter
        self._stop_event = threading.Event()
        self._wait = wait if wait is not None else self._stop_event.wait
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """Whether the monitor thread is alive.

        Returns:
            True while the thread started by `start` is running.
        """
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the thread, once.

        Returns:
            True if a thread was started. False when the interval is 0 or less (a `disabled`
            entry is logged), or the monitor already runs or was stopped.

        Side Effects:
            Starts a daemon thread and emits a `drift_monitor` entry.
        """
        fields: dict[str, str | int | float] = {"interval_seconds": self._interval}
        if self._interval <= 0:
            emit_gateway_event("drift_monitor", "disabled", fields, emitter=self._emitter)
            return False
        with self._lock:
            if self._thread is not None or self._stop_event.is_set():
                return False
            self._thread = threading.Thread(
                target=self._run, name=DRIFT_MONITOR_THREAD_NAME, daemon=True
            )
            self._thread.start()
        emit_gateway_event("drift_monitor", "started", fields, emitter=self._emitter)
        return True

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> bool:
        """Ask the thread to stop and wait up to `timeout` seconds.

        Args:
            timeout: Seconds to wait for the thread to end.

        Returns:
            True if the thread ended, or was never started; False if it was still running.

        Side Effects:
            Sets the stop event and emits a `drift_monitor` entry (`stopped` or `not_stopped`).
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        stopped = not thread.is_alive()
        fields: dict[str, str | int | float] = {"interval_seconds": self._interval}
        emit_gateway_event(
            "drift_monitor", "stopped" if stopped else "not_stopped", fields, emitter=self._emitter
        )
        return stopped

    def _run(self) -> None:
        """Wait one interval, run a pass, repeat until stopped; a bad pass never ends the thread."""
        while not self._stop_event.is_set():
            if self._wait(self._interval):
                return
            try:
                self._run_pass()
            except Exception as exc:  # noqa: BLE001  # the thread must survive one bad pass; it is logged
                fields: dict[str, str | int | float] = {"error_class": type(exc).__name__}
                emit_gateway_event("drift_pass", "failed", fields, emitter=self._emitter)
