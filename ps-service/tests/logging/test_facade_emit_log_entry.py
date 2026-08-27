"""Tests for ps_service.logging.facade.emit_log_entry (AC#1, AC#2, M15, M7/D9)."""

from __future__ import annotations

import threading
from pathlib import Path

from ps_service.logging import bind_run_context, emit_log_entry


def test_emit_log_entry_when_run_id_bound_then_baked_into_entry(make_emitter, read_lines) -> None:
    emitter, log_path = make_emitter()

    with bind_run_context("run-xyz"):
        emit_log_entry(component="ac1", action="do_thing", emitter=emitter)
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written — wiring bug"
    assert lines[0]["run_id"] == "run-xyz"


def test_emit_log_entry_when_two_threads_bind_and_emit_through_same_emitter_then_no_cross_contamination(
    make_emitter, read_lines
) -> None:
    emitter, log_path = make_emitter()
    entries_per_thread = 5

    def worker(tag: str) -> None:
        with bind_run_context(tag):
            for i in range(entries_per_thread):
                emit_log_entry(component="ac2", action=f"t{i}", emitter=emitter)  # same instance (M8 fix)

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("thread-a", "thread-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    emitter.flush()  # same instance the workers used (M8 fix)

    lines = read_lines(log_path)
    assert lines, "no entries were written — wiring bug, not proof of isolation (M8)"
    assert len(lines) == 2 * entries_per_thread
    run_ids_seen = {line["run_id"] for line in lines}
    assert run_ids_seen == {"thread-a", "thread-b"}  # disjoint, no foreign run_id in any line


def test_emit_log_entry_when_no_emitter_and_no_default_then_raises_logging_lifecycle_error() -> None:
    import pytest

    from ps_service.logging import LoggingLifecycleError

    with pytest.raises(LoggingLifecycleError):
        emit_log_entry(component="ac1", action="do_thing")


def test_atexit_drain_hook_registered_once_when_configure_called_multiple_times(tmp_path: Path) -> None:
    import atexit
    from unittest.mock import patch

    from ps_service.logging import configure

    with patch.object(atexit, "register") as mock_register:
        configure(tmp_path / "first.jsonl")
        configure(tmp_path / "second.jsonl")

    assert mock_register.call_count == 1
