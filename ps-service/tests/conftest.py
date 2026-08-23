"""Shared fixtures for all `ps_service` tests.

Isolates every test from the real, gitignored `logs/ps-service.jsonl` sink:
`main.py`'s `lifespan` calls the real `Logging.configure()`, so without this
isolation every test run touching the entrypoint would append to that file.
Mirrors the pattern already established in `tests/logging/conftest.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from ps_service.logging.facade import reset_for_tests


@pytest.fixture(autouse=True)
def _isolate_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Redirect `PS_LOGGING_DIR` to a per-test `tmp_path` and reset the facade around each test.

    Ensures no test ever reads leftover state from a previous test and no
    test call to `Logging.configure()`/`emit_log_entry` leaks into the real
    `logs/ps-service.jsonl`.
    """
    monkeypatch.setenv("PS_LOGGING_DIR", str(tmp_path))
    reset_for_tests()
    yield
    reset_for_tests()
