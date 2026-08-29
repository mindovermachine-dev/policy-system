"""Tests for the writer thread's failure-safe fallback path (AC#6, M3, M12, N1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.logging import EmitterConfig, LogEmitter
from ps_service.logging.models import LogEntry

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from conftest import ReadLines


def test_emit_when_target_unwritable_then_falls_back_to_stderr_via_capsys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocking_file = tmp_path / "not_a_dir"
    blocking_file.write_text("x")  # parent-to-be is a regular file -> mkdir fails inside the writer
    log_path = blocking_file / "x.log"
    emitter = LogEmitter(
        EmitterConfig(log_path=log_path)
    )  # fallback=None -> resolves sys.stderr dynamically

    emitter.emit(LogEntry(component="ac6", action="unwritable"))
    emitter.flush()  # N1: fallback write completes on the writer thread BEFORE this barrier returns

    captured = (
        capsys.readouterr()
    )  # N1: read only AFTER flush() — ordering is enforced, not assumed
    assert "ac6" in captured.err
    assert not log_path.exists()  # M12 tie-in: mkdir failed before any file was created


def test_emit_when_target_unwritable_then_falls_back_to_injected_stringio(tmp_path: Path) -> None:
    from io import StringIO

    blocking_file = tmp_path / "not_a_dir"
    blocking_file.write_text("x")
    log_path = blocking_file / "x.log"
    fallback = StringIO()
    emitter = LogEmitter(EmitterConfig(log_path=log_path, fallback=fallback))

    emitter.emit(LogEntry(component="ac6", action="unwritable"))
    emitter.flush()

    assert "ac6" in fallback.getvalue()  # AC#6's non-flaky fail-safe assertion


def test_emit_when_extra_not_json_serializable_then_falls_back_and_writer_survives(
    tmp_path: Path, read_lines: ReadLines
) -> None:
    log_path = tmp_path / "test.jsonl"
    emitter = LogEmitter(EmitterConfig(log_path=log_path))

    bad_entry = LogEntry(
        component="ac3", action="bad", extra=(("ts", object()),)
    )  # not JSON-serializable
    good_entry = LogEntry(component="ac3", action="good")

    emitter.emit(bad_entry)
    emitter.emit(good_entry)
    # both handled — if the writer had died, this would hang (proves M6+M3 together)
    emitter.flush()

    lines = read_lines(log_path)
    assert any(
        line.get("action") == "good" for line in lines
    )  # writer kept running after the bad entry


def test_write_failure_when_target_missing_parent_then_log_file_never_created(
    tmp_path: Path,
) -> None:
    from io import StringIO

    blocking_file = tmp_path / "not_a_dir"
    blocking_file.write_text("x")
    log_path = blocking_file / "x.log"
    fallback = StringIO()
    emitter = LogEmitter(EmitterConfig(log_path=log_path, fallback=fallback))

    emitter.emit(LogEntry(component="ac6", action="unwritable"))
    emitter.flush()

    assert not log_path.exists()  # M12 tie-in: the writer never got far enough to create the file
