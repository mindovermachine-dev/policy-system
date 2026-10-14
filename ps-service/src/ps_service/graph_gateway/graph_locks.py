"""In-process per-graph locks that serialise writers of one graph in the Graph Write Gateway.

Lock order (never reverse it): (1) the graph's in-process lock from this registry, then (2) the
Postgres `pg_advisory_xact_lock` taken inside `GraphLogStore.append_group`. A thread never
acquires (1) while holding (2) for the same graph. A thread that needs several graphs (a
multi-graph group) takes all their in-process locks at once through `lock_all`, in ascending
graph name, and the store then takes the advisory locks in the same order. Every thread that
holds more than one lock took them ascending from none held, and a single-lock holder never
takes a second, so no cycle can form. The in-process lock covers everything that must see a
stable graph: catch-up, preconditions, the state read, the append, the apply and the marker
advance. It is enough because the chart pins `replicas: 1`; the advisory lock keeps positions
gap-free regardless.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable


class GraphLockRegistry:
    """One `threading.Lock` per graph name, created on first use.

    Locks are never evicted: the registry is bounded by the number of distinct graphs a process
    writes to, which is small and fixed by the schema.

    Side Effects:
        `lock_for` creates and keeps a lock the first time a graph name is seen.
    """

    def __init__(self) -> None:
        """Create an empty registry; locks appear on first use."""
        self._mutex = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, graph: str) -> threading.Lock:
        """Return the lock that serialises writers of one graph.

        Args:
            graph: Graph name; any string, no validation is applied here.

        Returns:
            The `threading.Lock` of `graph`, created on first use; the same object for the same
            name on every call. The caller acquires and releases it.
        """
        with self._mutex:
            return self._locks.setdefault(graph, threading.Lock())

    @contextlib.contextmanager
    def lock_all(self, graphs: Iterable[str]) -> Generator[None]:
        """Hold the locks of every distinct graph in `graphs`, taken in ascending name order.

        The one way to hold more than one graph's lock: the fixed order is what rules out a
        deadlock between two such holders.

        Args:
            graphs: Graph names; duplicates are collapsed and an empty iterable holds nothing.

        Yields:
            None, while every lock is held. The locks are released in the reverse order on exit,
            also when the body raises.

        Side Effects:
            Blocks until every lock is acquired.
        """
        with contextlib.ExitStack() as held:
            for graph in sorted(set(graphs)):
                held.enter_context(self.lock_for(graph))
            yield
