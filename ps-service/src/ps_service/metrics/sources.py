"""Bounded, cached access to a slow metric source, and the pending-entries bounds.

A scrape must stay cheap and bounded however slow or broken a source is. `TtlCachedSource`
caches both a value and a failure for a TTL, so a scrape loop never amplifies load on a down
dependency, and lets one call run at a time.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.metrics.errors import SourceDeadlineExceededError
from ps_service.metrics.observers import log_source_failure

if TYPE_CHECKING:
    from collections.abc import Callable

PENDING_CONNECT_TIMEOUT_S = 2.0
PENDING_STATEMENT_TIMEOUT_MS = 2000
PENDING_SOURCE_DEADLINE_S = 5.0
"""Longest a scrape waits for the pending-entries source: above connect plus statement (4 s)."""
PENDING_TTL_S = 15.0


@dataclass
class _Entry[T]:
    stamp: float
    value: T | None


@dataclass
class _Flight[T]:
    future: Future[T]
    started: float
    timed_out: bool = False


class TtlCachedSource[T]:
    """Cache a source's value, or its failure, for a TTL; allow one call at a time.

    A call that outlasts the deadline is reported as failed to callers, but it keeps the
    single-flight slot until its thread returns, so a hung driver is never joined by a second
    call. A late success is cached when it lands. Failures are logged once per refresh as a
    semantic entry carrying the exception class only.
    """

    def __init__(
        self,
        name: str,
        fetch: Callable[[], T],
        *,
        ttl_seconds: float,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wrap `fetch`.

        Args:
            name: Short source name; failures log as the `collect_<name>` action.
            fetch: Returns the value; may block, may raise.
            ttl_seconds: How long a value or a failure is reused.
            timeout_seconds: Deadline for one call, measured from when it started.
            clock: Monotonic time source; injected by tests.
        """
        self._name = name
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entry: _Entry[T] | None = None
        self._flight: _Flight[T] | None = None

    def get(self) -> T | None:
        """Return the cached value, refreshing it when stale.

        Returns:
            The value, or `None` when the latest refresh failed, timed out or is still hung.

        Side Effects:
            May start one daemon thread that calls the source; logs a failed refresh once.
        """
        with self._lock:
            entry = self._entry
            if entry is not None and self._clock() - entry.stamp < self._ttl:
                return entry.value
            flight = self._flight or self._start()
        return self._wait(flight)

    def _start(self) -> _Flight[T]:
        """Begin the one in-flight call (the lock is held)."""
        flight = _Flight(future=Future[T](), started=self._clock())
        self._flight = flight
        threading.Thread(target=self._run, args=(flight,), daemon=True).start()
        return flight

    def _run(self, flight: _Flight[T]) -> None:
        """Call the source on the worker thread and publish its outcome."""
        try:
            value = self._fetch()
        except Exception as exc:  # noqa: BLE001 -- any source failure is cached as no value
            self._finish(flight, None)
            if not flight.timed_out:
                log_source_failure(self._name, exc)
            flight.future.set_exception(exc)
        else:
            self._finish(flight, value)
            flight.future.set_result(value)

    def _finish(self, flight: _Flight[T], value: T | None) -> None:
        """Cache the outcome and free the single-flight slot."""
        with self._lock:
            self._entry = _Entry(self._clock(), value)
            if self._flight is flight:
                self._flight = None

    def _wait(self, flight: _Flight[T]) -> T | None:
        """Wait for `flight` until its deadline; give up on it when that passes."""
        remaining = max(self._timeout - (self._clock() - flight.started), 0.0)
        try:
            return flight.future.result(timeout=remaining)
        except Exception:  # noqa: BLE001 -- failure was already cached and logged by the worker
            if flight.future.done():
                return None
        return self._give_up(flight)

    def _give_up(self, flight: _Flight[T]) -> None:
        """Cache a timeout failure for the TTL, once per flight."""
        with self._lock:
            first = not flight.timed_out and self._flight is flight
            if first:
                flight.timed_out = True
                self._entry = _Entry(self._clock(), None)
        if first:
            log_source_failure(self._name, SourceDeadlineExceededError())
