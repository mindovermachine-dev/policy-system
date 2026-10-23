"""In-process registry and launcher for background ingestion runs.

Same shape as `ps_service.api.run_status`: one module-level lock and one dict. A run's slot is
reserved before its row is written, held while its worker thread runs, and released when the
worker ends, however it ends. The worker writes its terminal row *before* the slot is
released, so "row still `running` and no slot held" reliably means "no worker is writing it".

Threads are daemons so a graceful shutdown never waits on a multi-minute ingestion; a run
cut short that way keeps a `running` row, which `_reconcile_orphaned_run` in
`ps_service.mcp_interface.ingestion_tools` later reconciles.
The registry is process-local: it assumes the single-replica deployment.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import TYPE_CHECKING

from ps_service.ingestion_runs.errors import (
    IngestionRunAlreadyInProgressError,
    IngestionRunCapacityExceededError,
)
from ps_service.logging.errors import LoggingLifecycleError
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Callable

_COMPONENT = "ingestion_runs"
_JOIN_TIMEOUT_SECONDS = 30.0


@dataclasses.dataclass(slots=True)
class _InFlightRun:
    """One reserved slot, keyed by run id in `_in_flight`."""

    short_name: str


_lock = threading.Lock()
_in_flight: dict[str, _InFlightRun] = {}
_threads: dict[str, threading.Thread] = {}  # kept after release so tests can join a finished run


def reserve_run_slot(run_id: str, *, short_name: str, max_in_flight_runs: int) -> None:
    """Atomically admit `run_id` and reserve its in-flight slot.

    Bounds concurrent ingestions and prevents two runs of the same instrument. A second
    in-flight run for the same `short_name` is rejected first, so a duplicate gets the more
    specific message even when the cap is also reached.

    Args:
        run_id: Identifier of the run; the registry key.
        short_name: Instrument short name; at most one in-flight run may hold it.
        max_in_flight_runs: Configured cap on simultaneously held slots.

    Raises:
        IngestionRunAlreadyInProgressError: a run for `short_name` already holds a slot.
        IngestionRunCapacityExceededError: `max_in_flight_runs` runs already hold slots.

    Side Effects:
        Adds `run_id` to the process-local in-flight registry.
    """
    with _lock:
        if any(run.short_name == short_name for run in _in_flight.values()):
            raise IngestionRunAlreadyInProgressError(short_name)
        if len(_in_flight) >= max_in_flight_runs:
            raise IngestionRunCapacityExceededError(max_in_flight_runs)
        _in_flight[run_id] = _InFlightRun(short_name=short_name)


def release_run_slot(run_id: str) -> None:
    """Free `run_id`'s slot; a no-op when it holds none.

    Args:
        run_id: Identifier of the run whose slot is released.

    Side Effects:
        Removes `run_id` from the process-local in-flight registry.
    """
    with _lock:
        _in_flight.pop(run_id, None)


def _log(action: str, outcome: str, run_id: str) -> None:
    try:
        emit_log_entry(component=_COMPONENT, action=action, outcome=outcome, run_id=run_id)
    except LoggingLifecycleError:
        return  # diagnostics only: a process with no configured emitter must still run work


def start_background_run(run_id: str, work: Callable[[], None]) -> None:
    """Run `work` on a new daemon thread, releasing `run_id`'s slot when it ends.

    The slot must already be reserved with `reserve_run_slot`. It is released however `work`
    ends, after `work` has written its terminal row.

    Args:
        run_id: Identifier of the run; names the thread and keys the slot.
        work: Callable that performs the run and records its terminal outcome.

    Raises:
        RuntimeError: the thread could not be started; the slot is released first.

    Side Effects:
        Starts a daemon thread, records it for test joins and emits
        `background_run_started` / `background_run_finished` log entries.
    """

    def _run_then_release() -> None:
        try:
            work()
        finally:
            release_run_slot(run_id)
            _log("background_run_finished", "success", run_id)

    thread = threading.Thread(target=_run_then_release, name=f"ingestion-run-{run_id}", daemon=True)
    with _lock:
        _threads[run_id] = thread
    try:
        thread.start()
    except RuntimeError:
        release_run_slot(run_id)
        raise
    _log("background_run_started", "success", run_id)


def is_run_in_flight(run_id: str) -> bool:
    """Return whether `run_id` currently holds a slot.

    Args:
        run_id: Identifier of the run to look up.

    Returns:
        True while the run's slot is reserved, false otherwise.
    """
    with _lock:
        return run_id in _in_flight


def in_flight_run_count() -> int:
    """Return how many runs currently hold a slot.

    Returns:
        The number of reserved in-flight slots; zero when idle.
    """
    with _lock:
        return len(_in_flight)


def wait_for_tests(run_id: str, *, timeout_seconds: float) -> None:
    """Join `run_id`'s worker (test-only); fail if it is still alive afterwards.

    Args:
        run_id: Identifier of the run whose worker thread is joined.
        timeout_seconds: Maximum time to wait for the worker, in seconds.

    Raises:
        AssertionError: the worker is still alive after `timeout_seconds`.

    Side Effects:
        Blocks the caller until the worker ends or the timeout passes. Returns immediately
        when no worker was started for `run_id`.
    """
    with _lock:
        thread = _threads.get(run_id)
    if thread is None:
        return
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        message = f"ingestion run {run_id} did not finish in {timeout_seconds}s"
        raise AssertionError(message)


def reset_for_tests() -> None:
    """Join every recorded worker (bounded), then forget all slots (test-only).

    Side Effects:
        Blocks up to 30 seconds per recorded worker, then clears the in-flight and thread
        registries.
    """
    with _lock:
        threads = list(_threads.values())
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    with _lock:
        _in_flight.clear()
        _threads.clear()
