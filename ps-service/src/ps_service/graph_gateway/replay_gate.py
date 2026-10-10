"""The write gate of graphs whose replay is running or has failed (issue #207, AC-RD-006/011).

A graph is closed from the moment its replay starts until a replay of it completes. A replay that
fails leaves the graph closed with the failed position remembered; one that is interrupted (an
infrastructure error, a stop) leaves it closed as incomplete. While a startup replay runs, every
graph is closed until the replay has dealt with it (`begin_all_pending`, `release`), including
graphs the log has never seen, and the whole hold ends only when the startup replay finished
(`end_all_pending`). Nothing here repairs a graph: only a later replay that completes opens a
failed one again.
"""

from __future__ import annotations

import threading

from ps_service.graph_gateway.errors import GraphReplayGatedError


class ReplayGate:
    """Thread-safe record of the graphs that refuse writes because of a replay."""

    def __init__(self) -> None:
        """Start with every graph open."""
        self._mutex = threading.Lock()
        self._incomplete: set[str] = set()
        self._failed: dict[str, int] = {}
        self._all_pending = False
        self._released: set[str] = set()

    def begin(self, graph: str) -> None:
        """Close `graph` for a replay that is starting (a remembered failure is superseded)."""
        with self._mutex:
            self._incomplete.add(graph)
            self._failed.pop(graph, None)

    def complete(self, graph: str) -> None:
        """Open `graph`: a replay of it completed."""
        with self._mutex:
            self._incomplete.discard(graph)
            self._failed.pop(graph, None)

    def fail(self, graph: str, position: int) -> None:
        """Keep `graph` closed because its replay failed at log position `position`."""
        with self._mutex:
            self._incomplete.discard(graph)
            self._failed[graph] = position

    def begin_all_pending(self) -> None:
        """Close every graph until a startup replay has dealt with it (see `release`)."""
        with self._mutex:
            self._all_pending = True
            self._released.clear()

    def release(self, graph: str) -> None:
        """Let `graph` out of the startup hold (it is still closed if its replay failed)."""
        with self._mutex:
            self._released.add(graph)

    def end_all_pending(self) -> None:
        """End the startup hold: the startup replay dealt with every logged graph."""
        with self._mutex:
            self._all_pending = False
            self._released.clear()

    def is_gated(self, graph: str) -> bool:
        """Whether `graph` currently refuses writes."""
        with self._mutex:
            return self._closed(graph)

    def failed_graphs(self) -> frozenset[str]:
        """The graphs whose replay failed and which stay closed until a later replay completes."""
        with self._mutex:
            return frozenset(self._failed)

    def require_open(self, graph: str) -> None:
        """Raise `GraphReplayGatedError` unless `graph` accepts writes."""
        with self._mutex:
            failed_at = self._failed.get(graph)
            closed = self._closed(graph)
        if failed_at is not None:
            raise GraphReplayGatedError(graph, failed_at, failed=True)
        if closed:
            raise GraphReplayGatedError(graph)

    def _closed(self, graph: str) -> bool:
        """Whether `graph` is closed; the caller holds the mutex."""
        held_by_startup = self._all_pending and graph not in self._released
        return graph in self._incomplete or graph in self._failed or held_by_startup
