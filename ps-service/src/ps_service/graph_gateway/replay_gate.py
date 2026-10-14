"""The write gate of graphs whose replay is running or has failed.

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
    """Thread-safe record of the graphs that refuse writes because of a replay.

    Every method takes an internal mutex, so the gate may be shared by writer and replay threads.
    The gate only records state; it never touches a graph or the log.
    """

    def __init__(self) -> None:
        """Create a gate in which every graph is open and no startup hold is active."""
        self._mutex = threading.Lock()
        self._incomplete: set[str] = set()
        self._failed: dict[str, int] = {}
        self._all_pending = False
        self._released: set[str] = set()

    def begin(self, graph: str) -> None:
        """Close `graph` for a replay that is starting.

        Args:
            graph: Graph name. A remembered failure of this graph is forgotten: the new replay
                supersedes it.

        Side Effects:
            Marks `graph` as incomplete (closed) until `complete` or `fail` is called.
        """
        with self._mutex:
            self._incomplete.add(graph)
            self._failed.pop(graph, None)

    def complete(self, graph: str) -> None:
        """Open `graph` because a replay of it completed.

        Args:
            graph: Graph name. Clears both the incomplete mark and any remembered failure; a
                startup hold on all graphs can still keep it closed.

        Side Effects:
            Removes the incomplete and failed marks of `graph`.
        """
        with self._mutex:
            self._incomplete.discard(graph)
            self._failed.pop(graph, None)

    def fail(self, graph: str, position: int) -> None:
        """Keep `graph` closed because its replay failed.

        Args:
            graph: Graph name.
            position: Log position at which the replay failed; reported by `require_open` so the
                operator knows where the log stops being applicable.

        Side Effects:
            Replaces the incomplete mark of `graph` with the remembered failed position.
        """
        with self._mutex:
            self._incomplete.discard(graph)
            self._failed[graph] = position

    def begin_all_pending(self) -> None:
        """Close every graph, logged or not, until a startup replay has dealt with it.

        Side Effects:
            Starts the startup hold and forgets every earlier `release`; `release` lets single
            graphs out and `end_all_pending` ends the hold.
        """
        with self._mutex:
            self._all_pending = True
            self._released.clear()

    def release(self, graph: str) -> None:
        """Let `graph` out of the startup hold.

        Args:
            graph: Graph name. It stays closed if its replay is incomplete or failed.

        Side Effects:
            Records `graph` as released; has no effect while no startup hold is active.
        """
        with self._mutex:
            self._released.add(graph)

    def end_all_pending(self) -> None:
        """End the startup hold because the startup replay dealt with every logged graph.

        Side Effects:
            Ends the hold and forgets the released graphs; failed and incomplete graphs stay
            closed.
        """
        with self._mutex:
            self._all_pending = False
            self._released.clear()

    def is_gated(self, graph: str) -> bool:
        """Report whether `graph` currently refuses writes.

        Args:
            graph: Graph name.

        Returns:
            True when the graph is incomplete, failed, or held by the startup hold; False when it
            accepts writes.
        """
        with self._mutex:
            return self._closed(graph)

    def failed_graphs(self) -> frozenset[str]:
        """List the graphs whose replay failed and which stay closed until a later replay completes.

        Returns:
            A snapshot set of graph names; empty when no replay has failed.
        """
        with self._mutex:
            return frozenset(self._failed)

    def require_open(self, graph: str) -> None:
        """Refuse a write to a closed graph.

        Args:
            graph: Graph name the caller is about to write.

        Raises:
            GraphReplayGatedError: `graph` is closed. The error carries the failed position and
                `failed=True` when the replay failed, and is plain when the replay is merely
                running, incomplete or held by startup.
        """
        with self._mutex:
            failed_at = self._failed.get(graph)
            closed = self._closed(graph)
        if failed_at is not None:
            raise GraphReplayGatedError(graph, failed_at, failed=True)
        if closed:
            raise GraphReplayGatedError(graph)

    def _closed(self, graph: str) -> bool:
        """Report whether `graph` is closed; the caller holds the mutex."""
        held_by_startup = self._all_pending and graph not in self._released
        return graph in self._incomplete or graph in self._failed or held_by_startup
