"""Re-probe of the gating dependencies that `/readyz` currently reports unhealthy.

Real calls are what normally flip a dependency back to healthy in `ps_service.dependency_health`,
but a pod that reports not ready receives no traffic, so nothing would. `GatingReprobe` lets each
`/readyz` poll run the dependency's own connectivity probe instead. Every real probe records its
own outcome in the registry; this harness never writes the registry, it only calls the probe and
reads the registry afterwards.

Each probe runs on its own daemon thread, so a hung dependency never occupies the event loop or a
shared worker thread. At most one probe per dependency is in flight at a time; every poll that
arrives while it runs joins it. A new probe starts only once the minimum interval, counted from
the start of the dependency's previous probe, has passed; a poll inside the interval starts
nothing and reads the registry as it stands. The first probe of a dependency is never held back.
A poll waits for the probes it needs together, under one deadline, and gives up on them when it
passes; the probe keeps running and its slot stays taken until it returns.

Logging is once per transition, never per poll: the first failed or timed-out probe of an outage
writes one `failed` entry carrying the exception class name only, and the first sign afterwards
that the dependency is healthy again writes one `succeeded` entry.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ps_service.logging import LoggingLifecycleError, emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable


_COMPONENT = "entrypoint"
_ACTION = "ready_reprobe"


@dataclass
class _Flight:
    """The one probe in flight for a dependency, and the polls waiting on it."""

    name: str
    waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = field(
        default_factory=list
    )


def _log_transition(name: str, outcome: str, error_class: str | None) -> None:
    """Write one `ready_reprobe` entry for `name`; a process without a log emitter is tolerated."""
    extra: dict[str, object] = {"dependency": name}
    if error_class is not None:
        extra["error_class"] = error_class
    with contextlib.suppress(LoggingLifecycleError):
        emit_log_entry(component=_COMPONENT, action=_ACTION, outcome=outcome, extra=extra)


def _resolve(waiter: asyncio.Future[None]) -> None:
    """Complete `waiter` unless its poll already gave up or was cancelled."""
    if not waiter.done():
        waiter.set_result(None)


class GatingReprobe:
    """Probe each unhealthy gating dependency once per flight, shared by every waiting poll."""

    def __init__(
        self,
        *,
        dependencies: Callable[[], tuple[str, ...]],
        probe_for: Callable[[str], Callable[[], None] | None],
        is_healthy: Callable[[str], bool],
        min_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire the harness to the gating set, the probe table and the health registry.

        Args:
            dependencies: Returns the gating dependency names; read on every refresh.
            probe_for: Returns the zero-argument connectivity probe for a name, or `None` when the
                dependency has none (its state is owned elsewhere). A probe raises when the
                dependency is unreachable and records its own outcome in the registry. Looked up
                on every refresh.
            is_healthy: Whether the registry currently records the dependency as healthy.
            min_interval_seconds: Shortest time between the starts of two probes of the same
                dependency; zero disables the limit.
            clock: Monotonic time source for the interval; injected by tests.
        """
        self._dependencies = dependencies
        self._probe_for = probe_for
        self._is_healthy = is_healthy
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._last_started: dict[str, float] = {}
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}
        self._outage_logged: set[str] = set()

    async def refresh(self, deadline_at: float) -> None:
        """Probe every unhealthy gating dependency and wait for the probes, up to a deadline.

        All needed probes are awaited together, so several hung dependencies cost one deadline.
        A dependency whose last probe started less than the minimum interval ago, and has since
        finished, is not probed again. Never raises: a failing, hung or missing probe leaves the
        registry as it was, and cancelling the caller affects only the caller.

        Args:
            deadline_at: Event loop time (`loop.time()`) by which this poll must stop waiting.

        Side Effects:
            May start one daemon thread per unhealthy gating dependency that has no probe in
            flight; each calls its dependency and, through the probe, updates the registry.
            Enqueues at most one log entry per dependency per outage start and per recovery.
        """
        loop = asyncio.get_running_loop()
        joined: list[tuple[_Flight, asyncio.Future[None]]] = []
        for name in self._dependencies():
            if self._is_healthy(name):
                self._note_recovered(name)
                continue
            probe = self._probe_for(name)
            entry = None if probe is None else self._join(name, probe, loop)
            if entry is not None:
                joined.append(entry)
        if not joined:
            return
        try:
            await asyncio.wait(
                [waiter for _, waiter in joined], timeout=max(0.0, deadline_at - loop.time())
            )
            timed_out = [flight.name for flight, waiter in joined if not waiter.done()]
        finally:
            self._leave(joined)
        for name in timed_out:
            self._note_failed(name, TimeoutError.__name__)

    def _join(
        self, name: str, probe: Callable[[], None], loop: asyncio.AbstractEventLoop
    ) -> tuple[_Flight, asyncio.Future[None]] | None:
        """Add a waiter to the flight for `name`, starting one when allowed.

        Returns:
            The flight and this poll's waiter, or `None` when no flight is running and the last
            one started less than the minimum interval ago.
        """
        waiter: asyncio.Future[None] = loop.create_future()
        with self._lock:
            flight = self._flights.get(name)
            start = flight is None
            if flight is None:
                now = self._clock()
                last_started = self._last_started.get(name)
                if last_started is not None and now - last_started < self._min_interval:
                    return None
                self._last_started[name] = now
                flight = self._flights[name] = _Flight(name)
            flight.waiters.append((loop, waiter))
        if start:
            threading.Thread(target=self._run, args=(name, probe, flight), daemon=True).start()
        return flight, waiter

    def _leave(self, joined: list[tuple[_Flight, asyncio.Future[None]]]) -> None:
        """Remove this poll's waiters from their flights and cancel the ones still pending."""
        with self._lock:
            for flight, waiter in joined:
                flight.waiters = [entry for entry in flight.waiters if entry[1] is not waiter]
        for _, waiter in joined:
            waiter.cancel()

    def _run(self, name: str, probe: Callable[[], None], flight: _Flight) -> None:
        """Call `probe` on the worker thread, free the slot and wake the waiting polls."""
        error_class: str | None = None
        try:
            probe()
        except Exception as exc:  # noqa: BLE001 - a dependency outage must never fail a poll; the probe already recorded it
            error_class = type(exc).__name__
        if error_class is not None:
            self._note_failed(name, error_class)
        else:
            self._note_recovered(name)
        with self._lock:
            waiters, flight.waiters = flight.waiters, []
            if self._flights.get(name) is flight:
                del self._flights[name]
        for loop, waiter in waiters:
            with contextlib.suppress(RuntimeError):  # the poll's event loop is already closed
                loop.call_soon_threadsafe(_resolve, waiter)

    def _note_failed(self, name: str, error_class: str) -> None:
        """Log the start of an outage once; later failures of the same outage log nothing."""
        with self._lock:
            if name in self._outage_logged:
                return
            self._outage_logged.add(name)
        _log_transition(name, "failed", error_class)

    def _note_recovered(self, name: str) -> None:
        """Log the end of a logged outage once, when the registry shows the dependency healthy."""
        if not self._is_healthy(name):
            return
        with self._lock:
            if name not in self._outage_logged:
                return
            self._outage_logged.discard(name)
        _log_transition(name, "succeeded", None)
