"""In-process per-graph locks of the Graph Write Gateway (issue #206, AC-BI-006).

Lock order (never reverse it): (1) the graph's in-process lock from this registry, then (2) the
Postgres `pg_advisory_xact_lock` taken inside `GraphLogStore.append_group`. A thread never
acquires (1) while holding (2) for the same graph. The in-process lock covers everything that
must see a stable graph: catch-up, preconditions, the state read, the append, the apply and the
marker advance. It is enough because the chart pins `replicas: 1`; the advisory lock keeps
positions gap-free regardless.
"""

from __future__ import annotations

import threading


class GraphLockRegistry:
    """One `threading.Lock` per graph name, created on first use.

    Locks are never evicted: the registry is bounded by the number of distinct graphs a process
    writes to, which is small and fixed by the schema.
    """

    def __init__(self) -> None:
        """Start with no locks."""
        self._mutex = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, graph: str) -> threading.Lock:
        """Return the lock of `graph`; the same object for the same name, always."""
        with self._mutex:
            return self._locks.setdefault(graph, threading.Lock())
