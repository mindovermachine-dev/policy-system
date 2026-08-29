"""Non-blocking, failure-safe log writing — the EmitLogEntry machinery.

Owns the queue, the single daemon writer thread, and the flush seam (AC#4,
AC#5, AC#6). The writer thread deliberately imports nothing from
`ps_service.logging.run_context`: reading the ContextVar here would attach
the *writer's* context instead of the *caller's* — the AC#1/AC#2 invariant
that identity is baked into `LogEntry.run_id` at `emit()` time, on the
caller's thread, not read again later. See run_context.py's docstring.
"""

from __future__ import annotations

import contextlib
import json
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ps_service.logging.errors import LoggingLifecycleError

if TYPE_CHECKING:
    from ps_service.logging.models import LogEntry


class TextSink(Protocol):
    """A minimal writable text sink — the surface the writer thread needs (M9 fix).

    Structural (duck-typed): `open(...)`'s `TextIOWrapper`, `io.StringIO`,
    and `sys.stderr` all satisfy this without inheriting from it.
    """

    def write(self, s: str, /) -> int:
        """Append `s` to the sink and return the number of characters written."""
        ...

    def flush(self) -> None:
        """Flush any buffered text to the underlying destination."""
        ...

    def close(self) -> None:
        """Close the sink, releasing its underlying file handle."""
        ...


class WriterFactory(Protocol):
    """Produces the primary `TextSink` a `LogEmitter` appends JSON lines to (M9 fix)."""

    def __call__(self, log_path: Path) -> TextSink:
        """Open `log_path` and return the `TextSink` the writer thread appends to."""
        ...


def default_writer_factory(log_path: Path) -> TextSink:
    """Open `log_path` for UTF-8 text append; the default `WriterFactory`."""
    return Path(log_path).open(
        mode="a", encoding="utf-8"
    )  # lifetime owned by the writer thread, not a `with` block


@dataclass(frozen=True, slots=True)
class EmitterConfig:
    """Configuration for a `LogEmitter`: where it writes (AC#5) and its fallback (AC#6)."""

    log_path: Path
    fallback: TextSink | None = None  # None -> resolve sys.stderr dynamically at failure time (N1)


@dataclass(frozen=True, slots=True)
class _Sentinel:
    """Internal flush/stop barrier marker placed on the writer queue (D7)."""

    event: threading.Event
    is_stop: bool


class LogEmitter:
    """Owns an unbounded queue and one daemon writer thread (AC#4, AC#5, AC#6)."""

    def __init__(
        self, config: EmitterConfig, *, writer_factory: WriterFactory | None = None
    ) -> None:
        """Start the daemon writer thread immediately; construction is cheap and never raises."""
        self._config = config
        self._writer_factory = (
            writer_factory if writer_factory is not None else default_writer_factory
        )
        self._queue: queue.Queue[LogEntry | _Sentinel] = queue.Queue()
        self._stopped = False
        self._lifecycle_lock = threading.Lock()
        self._thread = threading.Thread(
            target=_run_writer_loop,
            args=(self._queue, config.log_path, self._writer_factory, config.fallback),
            name="ps-service-log-writer",
            daemon=True,
        )
        self._thread.start()

    def emit(self, entry: LogEntry) -> None:
        """Enqueue `entry` for the writer thread. O(1); never touches the filesystem (AC#4).

        `queue.Queue.put` briefly takes the queue's internal lock, shared
        with the writer's `get`; each critical section is a pointer swap, so
        this stays well under the 10 ms SLA even under contention (M7 caveat,
        documented here rather than silently assumed).
        """
        self._queue.put(entry)

    def flush(self, *, timeout: float = 5.0) -> None:
        """Block until every entry enqueued before this call has been written (D7/G3).

        FIFO queue + single writer + "the writer always acks a sentinel, even
        after a failed entry" (M6, see `_run_writer_loop`) together guarantee:
        event-set implies every prior entry has been handled (written, or
        safely fallback-logged on failure). Bounded by `timeout` — raises
        `LoggingLifecycleError` rather than blocking forever if the writer
        never acks (e.g. `flush()` called after `stop()`: the sentinel is
        enqueued but the exited writer thread will never pop it, so the wait
        times out instead of hanging — this is how M6's "flush after stop"
        hazard resolves without a separate stopped-check).
        """
        sentinel = _Sentinel(event=threading.Event(), is_stop=False)
        self._queue.put(sentinel)
        if not sentinel.event.wait(timeout=timeout):
            raise LoggingLifecycleError(
                f"flush() timed out after {timeout}s waiting for the writer thread"
            )

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the daemon writer thread, first draining anything already queued.

        Idempotent (M6 fix): a second `stop()` call is a no-op rather than
        enqueueing a sentinel the (already-exited) writer will never pop.
        The sentinel's event is set once the queue is drained up to the stop
        marker; `join(timeout=...)` additionally waits for the thread to
        finish closing its sink, so `stop()` does not return before the
        writer has fully exited (or the timeout elapses).
        """
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
        sentinel = _Sentinel(event=threading.Event(), is_stop=True)
        self._queue.put(sentinel)
        sentinel.event.wait(timeout=timeout)
        self._thread.join(timeout=timeout)


def _run_writer_loop(
    entry_queue: queue.Queue[LogEntry | _Sentinel],
    log_path: Path,
    writer_factory: WriterFactory,
    configured_fallback: TextSink | None,
) -> None:
    """Drain `entry_queue` until a stop sentinel; never die on an exception (M3, M6)."""
    sink: TextSink | None = None
    while True:
        item = entry_queue.get()
        try:
            if not isinstance(item, _Sentinel):
                sink = _append_line(item.to_json_line(), sink, log_path, writer_factory)
        except Exception as exc:  # noqa: BLE001 - the writer thread must never die (M3)
            _write_fallback(configured_fallback, _safe_fallback_line(item, exc))
        finally:
            if isinstance(item, _Sentinel):
                item.event.set()  # ALWAYS ack the sentinel, even after a failure above (M6)
            entry_queue.task_done()
        if isinstance(item, _Sentinel) and item.is_stop:
            break
    if sink is not None:
        _close_sink(sink, configured_fallback)


def _append_line(
    line: str, sink: TextSink | None, log_path: Path, writer_factory: WriterFactory
) -> TextSink:
    """Return an open sink for `log_path` (opening it lazily on first use) after writing `line`.

    Any failure here (parent-directory creation, open, write) propagates to
    the caller's `except Exception` in `_run_writer_loop` — deliberately one
    catch site handles serialize/open/write failures uniformly (M3). On
    failure, `sink` is never reassigned (the exception fires before the
    `return`), so the loop's local `sink` variable keeps its prior value.
    """
    if sink is None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        sink = writer_factory(log_path)
    sink.write(line)
    sink.flush()
    return sink


def _close_sink(sink: TextSink, configured_fallback: TextSink | None) -> None:
    """Best-effort close; a close failure falls back like any other write failure."""
    try:
        sink.close()
    except Exception as exc:  # noqa: BLE001 - shutdown path must not raise (M3)
        _write_fallback(
            configured_fallback, f"logging_error=close-failed: {type(exc).__name__}: {exc}\n"
        )


def _safe_fallback_line(item: LogEntry | _Sentinel, exc: Exception) -> str:
    """Best-effort JSON-safe fallback line for an entry that could not be handled normally.

    Built only from fields guaranteed to be JSON-serializable — excludes
    `extra`, the one caller-supplied field whose values might not be
    serializable (M3's "non-serializable extra" case: a `datetime`, `Path`,
    `set`, or custom object) — plus a description of the failure.
    """
    if isinstance(item, _Sentinel):
        return f'{{"logging_error": "sentinel handling failed: {type(exc).__name__}: {exc}"}}\n'
    safe: dict[str, object] = {
        "component": item.component,
        "action": item.action,
        "run_id": item.run_id,
        "entity_id": item.entity_id,
        "outcome": item.outcome,
        "duration_ms": item.duration_ms,
        "timestamp": item.timestamp,
        "logging_error": f"{type(exc).__name__}: {exc}",
    }
    try:
        return json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n"
    except Exception:  # noqa: BLE001 - absolute last resort, must not raise
        return f"logging_error={type(exc).__name__}: {exc}\n"


def _write_fallback(configured_fallback: TextSink | None, line: str) -> None:
    """Write `line` to the fallback sink, resolved dynamically (N1).

    Resolving dynamically lets `capsys` observe it even from the writer
    thread (a cached `sys.stderr` reference captured once at thread start
    would NOT be the object `capsys` swaps).
    Self-protected (M3, M6): a failing fallback sink falls back to a raw
    `sys.stderr` write, and even that is guarded — nothing in this function
    may propagate, or the daemon writer thread dies (M3's core defect).
    """
    sink = configured_fallback if configured_fallback is not None else sys.stderr
    try:
        sink.write(line)
        sink.flush()
    except Exception:  # noqa: BLE001 - last-resort sink; must not raise
        with contextlib.suppress(
            Exception
        ):  # truly last resort; nothing further can be done safely
            sys.stderr.write(line)
