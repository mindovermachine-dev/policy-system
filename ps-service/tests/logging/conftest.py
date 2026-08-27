"""Shared fixtures for ps_service.logging tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ps_service.logging import EmitterConfig, LogEmitter
from ps_service.logging.emitter import TextSink
from ps_service.logging.facade import reset_for_tests


@pytest.fixture(autouse=True)
def _reset_default_emitter() -> Iterator[None]:
    """Every test starts and ends with a clean facade default (isolation between tests)."""
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def make_emitter(tmp_path: Path):
    """Factory: a fresh `LogEmitter` writing to `tmp_path/test.jsonl`, plus its config.

    Returns a `(LogEmitter, Path)` pair so a test can flush the emitter and
    then read the same path.
    """

    def _make(*, filename: str = "test.jsonl", fallback: TextSink | None = None) -> tuple[LogEmitter, Path]:
        log_path = tmp_path / filename
        config = EmitterConfig(log_path=log_path, fallback=fallback)
        return LogEmitter(config), log_path

    return _make


@pytest.fixture
def read_lines():
    """Read `log_path` (after a `.flush()`) and parse each line as JSON."""

    def _read(log_path: Path) -> list[dict[str, object]]:
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]

    return _read
