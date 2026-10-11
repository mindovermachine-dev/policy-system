"""Tests for ``EmitterConfig.mirror_failures_to_stderr`` (issue #227, AC-BI-009).

A failed entry is the one an operator reads via ``kubectl logs``; with the option on, the writer
thread also writes its JSON line to stderr, in addition to the JSONL file.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import TYPE_CHECKING

from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.logging.models import LogEntry

if TYPE_CHECKING:
    from pathlib import Path


def _entry(outcome: str) -> LogEntry:
    """A minimal entry with the given outcome."""
    return LogEntry(
        component="api",
        action="ingestion_run",
        outcome=outcome,
        timestamp=1_767_225_600.0,
        extra=(("failing_stage", "merge"),),
    )


def test_a_failed_entry_is_mirrored_to_the_stderr_sink_when_enabled(tmp_path: Path) -> None:
    """With the option on, the failed line lands in both the file and the stderr sink."""
    sink = StringIO()
    log_path = tmp_path / "mirror.jsonl"
    emitter = LogEmitter(
        EmitterConfig(log_path=log_path, fallback=sink, mirror_failures_to_stderr=True)
    )

    emitter.emit(_entry("failed"))
    emitter.flush()

    mirrored = json.loads(sink.getvalue().strip())
    assert (mirrored["outcome"], mirrored["failing_stage"]) == ("failed", "merge")
    assert json.loads(log_path.read_text(encoding="utf-8").strip()) == mirrored


def test_a_non_failed_entry_is_not_mirrored(tmp_path: Path) -> None:
    """Only ``failed`` entries are mirrored; a succeeded one stays file-only."""
    sink = StringIO()
    emitter = LogEmitter(
        EmitterConfig(
            log_path=tmp_path / "mirror.jsonl", fallback=sink, mirror_failures_to_stderr=True
        )
    )

    emitter.emit(_entry("succeeded"))
    emitter.flush()

    assert sink.getvalue() == ""


def test_a_failed_entry_is_not_mirrored_by_default(tmp_path: Path) -> None:
    """The option defaults to off, so existing emitters keep their file-only behaviour."""
    sink = StringIO()
    emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "mirror.jsonl", fallback=sink))

    emitter.emit(_entry("failed"))
    emitter.flush()

    assert sink.getvalue() == ""
