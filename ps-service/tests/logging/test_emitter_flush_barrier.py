"""Tests for LogEmitter.flush/stop barrier semantics (D7/G3, and M6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import MakeEmitter, ReadLines


def test_flush_when_entries_queued_then_all_written_before_return(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    from ps_service.logging.models import LogEntry

    emitter, log_path = make_emitter()

    for i in range(5):
        emitter.emit(LogEntry(component="flush", action=f"entry-{i}"))
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written — flush() returned before the writer caught up"
    assert len(lines) == 5


def test_double_flush_does_not_hang(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    emitter.flush(timeout=2.0)
    emitter.flush(timeout=2.0)  # must not hang — pytest's own timeout would catch a regression here


def test_stop_when_called_twice_then_is_idempotent(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    emitter.stop(timeout=2.0)
    emitter.stop(timeout=2.0)  # no-op, must not raise or hang


def test_flush_when_writer_stopped_then_raises_logging_lifecycle_error(
    make_emitter: MakeEmitter,
) -> None:
    from ps_service.logging import LoggingLifecycleError

    emitter, _ = make_emitter()
    emitter.stop(timeout=2.0)
    import pytest

    with pytest.raises(LoggingLifecycleError):
        emitter.flush(timeout=0.5)  # bounded — fails fast instead of hanging
