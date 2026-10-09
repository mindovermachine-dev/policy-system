"""Background reconciler: applies committed-but-pending entries once FalkorDB recovers (S12).

A graph is registered when a group is reported `committed_apply_pending` (or a write finds the
graph lagging while FalkorDB is down). The thread starts lazily on the first registration, is a
daemon (it can never hold up shutdown), backs off between passes on the gateway's schedule capped
at `reconciler_max_backoff_seconds` and ends by itself when nothing is pending; registering a
graph starts a fresh one. `stop()` interrupts the backoff wait. Only graph names and error
classes are logged.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ps_service.graph_gateway.errors import GraphApplyError, GraphLogUnavailableError
from ps_service.graph_gateway.gateway_log import emit_gateway_event
from ps_service.graph_gateway.retry import backoff_seconds

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.graph_gateway.gateway import GatewaySettings
    from ps_service.graph_gateway.models import CatchUpResult
    from ps_service.logging import LogEmitter

RECONCILER_THREAD_NAME = "graph-gateway-reconciler"
DEFAULT_STOP_TIMEOUT_SECONDS = 5.0


class GraphReconciler:
    """Retries `catch_up` for registered graphs until each is caught up or refused."""

    def __init__(
        self,
        catch_up: Callable[[str], CatchUpResult],
        *,
        settings: GatewaySettings,
        emitter: LogEmitter | None,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        """Take the catch-up step and the schedule.

        `wait(seconds)` sleeps between passes and returns True if the reconciler must stop;
        by default it is `Event.wait` on the stop event, so `stop()` interrupts it.
        """
        self._catch_up = catch_up
        self._settings = settings
        self._emitter = emitter
        self._stop_event = threading.Event()
        self._wait = wait if wait is not None else self._stop_event.wait
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """Whether the reconciler thread is alive."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def register(self, graph: str) -> None:
        """Add `graph` to the pending set, starting the thread if it is not running."""
        with self._lock:
            self._pending.add(graph)
            if self._thread is None and not self._stop_event.is_set():
                self._thread = threading.Thread(
                    target=self._run, name=RECONCILER_THREAD_NAME, daemon=True
                )
                self._thread.start()

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS) -> bool:
        """Ask the thread to stop and wait up to `timeout` seconds; True if it ended.

        A thread that does not end is abandoned (it is a daemon) and the fact is logged.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        stopped = not thread.is_alive()
        if not stopped:
            emit_gateway_event("reconciler_stop", "abandoned", {}, emitter=self._emitter)
        return stopped

    def _run(self) -> None:
        """Back off, then reconcile every pending graph; repeat until none is left."""
        attempt = 1
        while not self._stop_event.is_set():
            if not self._has_pending():
                return
            if self._wait(min(self._delay(attempt), self._settings.reconciler_max_backoff_seconds)):
                return
            for graph in self._snapshot():
                if self._reconcile(graph):
                    self._discard(graph)
            attempt = attempt + 1 if self._has_pending() else 1

    def _delay(self, attempt: int) -> float:
        return backoff_seconds(self._settings, attempt)

    def _has_pending(self) -> bool:
        """Whether a graph is pending; if none is, retire the thread under the same lock."""
        with self._lock:
            if self._pending:
                return True
            self._thread = None
            return False

    def _snapshot(self) -> list[str]:
        with self._lock:
            return sorted(self._pending)

    def _discard(self, graph: str) -> None:
        with self._lock:
            self._pending.discard(graph)

    def _reconcile(self, graph: str) -> bool:
        """Run one pass for `graph`; True if it leaves the pending set (caught up or refused)."""
        try:
            result = self._catch_up(graph)
        except GraphApplyError as exc:
            self._log(graph, "refused", exc)
            return True
        except GraphLogUnavailableError as exc:
            self._log(graph, "pending", exc)
            return False
        except Exception as exc:  # noqa: BLE001  # a thread must survive one bad pass; it is logged
            self._log(graph, "failure", exc)
            return False
        self._log(graph, "success" if result.caught_up else "pending", None)
        return result.caught_up

    def _log(self, graph: str, outcome: str, error: BaseException | None) -> None:
        fields: dict[str, str | int | float] = {"graph": graph}
        if error is not None:
            fields["error_class"] = type(error).__name__
        emit_gateway_event("reconciler_pass", outcome, fields, emitter=self._emitter)
