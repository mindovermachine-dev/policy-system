"""Shared fixtures for ps_service.llm_interface tests.

`make_emitter`/`read_lines` are duplicated (not shared/imported) from
`tests/logging/conftest.py` — per L2's DRY rule ("extract once a pattern
repeats a third time"), this is only the 2nd occurrence, so local
duplication is the standard-sanctioned choice, not a shortcut. Needed by
AC-004/AC-005's tests (`test_route_completion_logs_run_id.py`,
`test_route_embedding_logs_run_id.py`), which must read back the JSON lines
a real `LogEmitter` wrote to assert the bound `run_id` was baked into the
entry.

The `emitter` fixture below is the throwaway-emitter pattern already
duplicated in `test_route_completion_mocked.py` and
`test_route_completion_live_provider.py`; `test_route_structured_completion_mocked.py`
is the third occurrence, so per the same DRY rule it is extracted here
instead of duplicated again. The two existing modules keep their local
copies untouched — a module-level fixture shadows a conftest one of the
same name, so nothing there needs to change.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol

import pytest

from ps_service.logging import EmitterConfig, LogEmitter
from ps_service.logging.facade import reset_for_tests

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from ps_service.logging.emitter import TextSink


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    """A real `LogEmitter` writing to a per-test tmp path.

    A `route_*` action's `log` call needs a live emitter (or a configured
    process default) or it raises `LoggingLifecycleError`; tests that don't
    assert on log content only need a throwaway emitter to satisfy that.
    """
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


class MakeEmitter(Protocol):
    """Factory returned by the `make_emitter` fixture."""

    def __call__(
        self, *, filename: str = ..., fallback: TextSink | None = ...
    ) -> tuple[LogEmitter, Path]:
        """Build a fresh `LogEmitter` writing under the test's `tmp_path`, plus its log path."""
        ...


class ReadLines(Protocol):
    """Reader returned by the `read_lines` fixture."""

    def __call__(self, log_path: Path) -> list[dict[str, object]]:
        """Read `log_path` (after a `.flush()`) and parse each non-empty line as JSON."""
        ...


@pytest.fixture(autouse=True)
def reset_default_emitter() -> Iterator[None]:
    """Every test starts and ends with a clean facade default (isolation between tests)."""
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def make_emitter(tmp_path: Path) -> MakeEmitter:
    """Factory: a fresh `LogEmitter` writing to `tmp_path/test.jsonl`, plus its config.

    Returns a `(LogEmitter, Path)` pair so a test can flush the emitter and
    then read the same path.
    """

    def _make(
        *, filename: str = "test.jsonl", fallback: TextSink | None = None
    ) -> tuple[LogEmitter, Path]:
        log_path = tmp_path / filename
        config = EmitterConfig(log_path=log_path, fallback=fallback)
        return LogEmitter(config), log_path

    return _make


@pytest.fixture
def read_lines() -> ReadLines:
    """Read `log_path` (after a `.flush()`) and parse each line as JSON."""

    def _read(log_path: Path) -> list[dict[str, object]]:
        if not log_path.exists():
            return []
        return [
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line
        ]

    return _read
