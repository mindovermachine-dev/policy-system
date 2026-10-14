"""Non-blocking, failure-safe log writing.

Purpose: own the queue, the single daemon writer thread and the flush and stop operations
behind `emit_log_entry`. Callers only enqueue; the writer thread appends JSON lines to the
log file and falls back to another sink on any failure, so logging never breaks the
caller's work. The module deliberately does not read the run-id context variable: the run id
is recorded in the entry when it is created on the caller's thread, and reading it in the
writer thread would attach the writer's context instead.
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
    """A minimal writable text sink: the surface the writer thread needs.

    Structural (duck-typed): a file opened for text, `io.StringIO` and `sys.stderr` all
    satisfy it without inheriting from it.
    """

    def write(self, s: str, /) -> int:
        """Append text to the sink.

        Args:
            s: The text to append; may contain newlines.

        Returns:
            The number of characters written.

        Raises:
            OSError: The underlying destination cannot be written (for example disk full or
                closed handle). The emitter's writer thread catches it and uses its fallback.

        Side Effects:
            Writes to the underlying destination, possibly buffered until `flush`.
        """
        ...

    def flush(self) -> None:
        """Flush any buffered text to the underlying destination.

        Returns:
            None.

        Raises:
            OSError: The underlying destination cannot be written. The emitter's writer
                thread catches it and uses its fallback.

        Side Effects:
            Forces buffered text out to the underlying destination.
        """
        ...

    def close(self) -> None:
        """Close the sink, releasing its underlying file handle.

        Returns:
            None.

        Raises:
            OSError: Closing failed. The emitter's writer thread catches it and reports it
                to its fallback sink.

        Side Effects:
            Flushes and releases the underlying destination; later writes fail.
        """
        ...


class WriterFactory(Protocol):
    """Produces the primary `TextSink` a `LogEmitter` appends JSON lines to."""

    def __call__(self, log_path: Path) -> TextSink:
        """Open the log destination and return the sink the writer thread appends to.

        Args:
            log_path: Path of the log file to open; its parent directory already exists.

        Returns:
            An open `TextSink` positioned to append. The writer thread owns it and closes it
            when the emitter stops.

        Raises:
            OSError: The destination cannot be opened. The writer thread catches it, writes
                the affected entry to its fallback sink and retries opening on the next entry.

        Side Effects:
            Opens (and may create) the destination.
        """
        ...


def default_writer_factory(log_path: Path) -> TextSink:
    """Open a log file for UTF-8 text append; the default `WriterFactory`.

    Args:
        log_path: Path of the log file; its parent directory must already exist.

    Returns:
        The open file object. The caller owns it and must close it.

    Raises:
        OSError: The file cannot be opened (missing directory, permission denied, and so on).

    Side Effects:
        Opens the file for appending, creating it if absent.
    """
    return Path(log_path).open(
        mode="a", encoding="utf-8"
    )  # lifetime owned by the writer thread, not a `with` block


@dataclass(frozen=True, slots=True)
class EmitterConfig:
    """Configuration for a `LogEmitter`: where it writes and where failures fall back to.

    Attributes:
        log_path: File the emitter appends JSON lines to. Its parent directory is created on
            first write if absent.
        fallback: Sink that receives lines the emitter cannot write to `log_path`. None
            resolves `sys.stderr` at the moment of failure.
        mirror_failures_to_stderr: When True, each entry whose outcome is ``"failed"`` is also
            written to the fallback sink (stderr when `fallback` is None). A log file inside
            a container is invisible to the container log stream, so mirroring puts the cause
            where an operator looks first.
    """

    log_path: Path
    fallback: TextSink | None = None  # None -> resolve sys.stderr dynamically at failure time
    mirror_failures_to_stderr: bool = False


@dataclass(frozen=True, slots=True)
class _Sentinel:
    """Internal flush or stop barrier marker placed on the writer queue."""

    event: threading.Event
    is_stop: bool


class LogEmitter:
    """Non-blocking log emitter: an unbounded queue drained by one daemon writer thread.

    Callers enqueue entries with `emit`; the writer thread appends them as JSON lines to the
    configured file, opening it lazily on first write. Any serialize, open or write failure
    is reported to the fallback sink and never ends the writer thread. Use `flush` to wait for
    queued entries to be written and `stop` to drain and shut the thread down.
    """

    def __init__(
        self, config: EmitterConfig, *, writer_factory: WriterFactory | None = None
    ) -> None:
        """Create the emitter and start its daemon writer thread immediately.

        Construction is cheap and performs no file I/O; the log file is opened on the first
        entry.

        Args:
            config: Log file path, fallback sink and mirroring choice.
            writer_factory: Opens the primary sink for the log path. None selects
                `default_writer_factory`; tests inject a fake.

        Side Effects:
            Starts a daemon thread named `ps-service-log-writer`.
        """
        self._config = config
        self._writer_factory = (
            writer_factory if writer_factory is not None else default_writer_factory
        )
        self._queue: queue.Queue[LogEntry | _Sentinel] = queue.Queue()
        self._stopped = False
        self._lifecycle_lock = threading.Lock()
        self._thread = threading.Thread(
            target=_run_writer_loop,
            args=(
                self._queue,
                config.log_path,
                self._writer_factory,
                config.fallback,
            ),
            kwargs={"mirror_failures": config.mirror_failures_to_stderr},
            name="ps-service-log-writer",
            daemon=True,
        )
        self._thread.start()

    def emit(self, entry: LogEntry) -> None:
        """Enqueue an entry for the writer thread without touching the filesystem.

        The enqueue takes the queue's internal lock briefly, which the writer's dequeue also
        takes. Each critical section is a pointer swap, so the call stays fast even under
        contention.

        Args:
            entry: The log entry to write.

        Returns:
            None.

        Side Effects:
            Adds the entry to the writer queue; the write happens later on the writer thread.
            After `stop()` entries are still accepted but never written.
        """
        self._queue.put(entry)

    def flush(self, *, timeout: float = 5.0) -> None:
        """Block until every entry enqueued before this call has been handled.

        The queue is first-in first-out with a single writer, and the writer always
        acknowledges the barrier even after a failed entry. So when the call returns, each
        earlier entry has been written or reported to the fallback sink. The wait is bounded;
        a flush after `stop()` times out instead of hanging, because the exited writer never
        acknowledges.

        Args:
            timeout: Maximum seconds to wait for the writer; must be positive. Defaults to 5.

        Returns:
            None.

        Raises:
            LoggingLifecycleError: The writer did not acknowledge within `timeout`, including
                when the emitter has already been stopped.

        Side Effects:
            Places a barrier marker on the writer queue and blocks the calling thread.
        """
        sentinel = _Sentinel(event=threading.Event(), is_stop=False)
        self._queue.put(sentinel)
        if not sentinel.event.wait(timeout=timeout):
            raise LoggingLifecycleError(
                f"flush() timed out after {timeout}s waiting for the writer thread"
            )

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the daemon writer thread after it drains everything already queued.

        The call is idempotent: a second call does nothing, rather than enqueueing a marker
        the exited writer would never take. It waits for the drain and then for the thread to
        finish closing its sink, so it does not return before the writer has exited unless
        the timeout elapses first.

        Args:
            timeout: Maximum seconds to wait for the drain, and again for the thread to exit;
                must be positive. Defaults to 5. Expiry does not raise.

        Returns:
            None.

        Side Effects:
            Places a stop marker on the writer queue, blocks the calling thread, and causes the
            writer thread to close the log file. Entries emitted afterwards are never written.
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
    *,
    mirror_failures: bool = False,
) -> None:
    """Drain the queue until a stop marker, writing entries and never dying on an exception.

    Each entry is rendered and appended to the log file through the lazily opened sink. Any
    failure is reported to the fallback sink and the loop continues. A flush or stop marker
    is always acknowledged, even if handling it failed, so waiters never hang. When a stop
    marker is reached the sink is closed.

    Args:
        entry_queue: Queue of entries and barrier markers shared with the emitter.
        log_path: Log file the primary sink writes to.
        writer_factory: Opens the primary sink on the first entry.
        configured_fallback: Fallback sink, or None for `sys.stderr` resolved at failure time.
        mirror_failures: When True, entries whose outcome is `"failed"` are also written to the
            fallback sink.

    Side Effects:
        Writes to the log file and fallback sink, and closes the log file when stopped.
    """
    sink: TextSink | None = None
    while True:
        item = entry_queue.get()
        try:
            if not isinstance(item, _Sentinel):
                line = item.to_json_line()
                if mirror_failures and item.outcome == "failed":
                    _write_fallback(configured_fallback, line)
                sink = _append_line(line, sink, log_path, writer_factory)
        except Exception as exc:  # noqa: BLE001 - the writer thread must never die
            _write_fallback(configured_fallback, _safe_fallback_line(item, exc))
        finally:
            if isinstance(item, _Sentinel):
                item.event.set()  # always acknowledge the marker, even after a failure above
            entry_queue.task_done()
        if isinstance(item, _Sentinel) and item.is_stop:
            break
    if sink is not None:
        _close_sink(sink, configured_fallback)


def _append_line(
    line: str, sink: TextSink | None, log_path: Path, writer_factory: WriterFactory
) -> TextSink:
    """Write one line to the log sink, opening it lazily on first use, and return the sink.

    A failure here (directory creation, open or write) propagates to the writer loop's single
    catch site, so serialize, open and write failures are handled uniformly. On failure the
    caller keeps its previous sink value, so a failed open is retried on the next entry.

    Args:
        line: The complete text line to append, including its newline.
        sink: The already open sink, or None when it has not been opened yet.
        log_path: Log file to open when `sink` is None; its parent directory is created.
        writer_factory: Opens the sink when `sink` is None.

    Returns:
        The open sink the line was written and flushed to.

    Raises:
        OSError: The directory cannot be created, or the sink cannot be opened, written or
            flushed.

    Side Effects:
        May create the parent directory and open the log file; writes and flushes the line.
    """
    if sink is None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        sink = writer_factory(log_path)
    sink.write(line)
    sink.flush()
    return sink


def _close_sink(sink: TextSink, configured_fallback: TextSink | None) -> None:
    """Close the sink on a best-effort basis; a failure is reported to the fallback sink.

    Args:
        sink: The open primary sink to close.
        configured_fallback: Fallback sink, or None for `sys.stderr` resolved at failure time.

    Side Effects:
        Closes the sink; on failure writes a `logging_error=close-failed` line to the fallback.
    """
    try:
        sink.close()
    except Exception as exc:  # noqa: BLE001 - shutdown path must not raise
        _write_fallback(
            configured_fallback, f"logging_error=close-failed: {type(exc).__name__}: {exc}\n"
        )


def _safe_fallback_line(item: LogEntry | _Sentinel, exc: Exception) -> str:
    """Build a best-effort fallback text line for an item that could not be handled normally.

    The line is built only from fields that are guaranteed to be JSON-serializable. It leaves
    out `extra`, the one caller-supplied field whose values may not be serializable (for
    example a `datetime`, `Path`, `set` or custom object), and adds a description of the
    failure.

    Args:
        item: The log entry, or barrier marker, whose handling failed.
        exc: The exception raised while handling it.

    Returns:
        A newline-terminated JSON object line, or, if even that cannot be serialized, a plain
        `logging_error=...` line.
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
    """Write a line to the fallback sink and never raise.

    When no fallback is configured, `sys.stderr` is looked up at call time rather than cached,
    so a test that swaps `sys.stderr` (for example with `capsys`) observes the output even from
    the writer thread. A failing fallback sink falls back to a raw `sys.stderr` write, and even
    that failure is suppressed, because an exception here would kill the writer thread.

    Args:
        configured_fallback: Fallback sink, or None for the current `sys.stderr`.
        line: The text to write, including its newline.

    Side Effects:
        Writes and flushes the fallback sink, or `sys.stderr` as a last resort.
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
