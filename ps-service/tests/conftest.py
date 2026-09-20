"""Shared fixtures for all `ps_service` tests.

Isolates every test from the real, gitignored `logs/ps-service.jsonl` sink:
`main.py`'s `lifespan` calls the real `Logging.configure()`, so without this
isolation every test run touching the entrypoint would append to that file.
Mirrors the pattern already established in `tests/logging/conftest.py`.

`make_emitter`/`read_lines` are defined here (not duplicated a third time)
per L2 DRY ("extract a shared function/class... once a pattern repeats a
third time"): `tests/logging/conftest.py` and `tests/llm_interface/
conftest.py` each already carry an identical local copy (the 1st and 2nd
occurrences, the latter's own docstring noting DRY's threshold wasn't yet
met); `tests/ingestion/test_pipeline.py`'s AC-005 run-id test is the third,
so this shared root-level copy is what it uses instead of adding a fourth
duplicate. The two pre-existing local copies are left as-is (out of scope
for this batch to touch); pytest resolves the nearer conftest.py first, so
they simply shadow this one for tests in their own directories, no
conflict.

Also registers `tests` itself as a real (if minimal) package in
`sys.modules` -- see `_register_tests_package_for_cross_package_imports`'s
own docstring for why (issue #58 Slice 3 regression).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ps_service.dependency_health import (
    reset_for_tests as reset_dependency_health_for_tests,
)
from ps_service.logging import EmitterConfig, LogEmitter
from ps_service.logging.facade import reset_for_tests

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.logging.emitter import TextSink


def _register_tests_package_for_cross_package_imports() -> None:
    """Make `tests` a real package in `sys.modules`, rooted at this directory.

    `ps-service/tests` deliberately has no `__init__.py` of its own (root
    `pyproject.toml`'s INP001 comment, tied to issue #52's fix for a
    same-basename collision across uv-workspace members) -- so under
    `--import-mode=importlib`, pytest never puts this directory on
    `sys.path` and never gives `tests` a package of its own. A same-package
    absolute import (e.g. `tests/restore/test_x.py` doing
    `from restore._fixtures import ...`) happens to work because pytest
    itself loads `restore/__init__.py` into `sys.modules` before running
    that test module. A *cross*-package import has no such guarantee: it
    depends on collection order, which is exactly why
    `tests/api/test_rest_auth_middleware.py`'s
    `from tests.auth.mock_oidc_provider import ...` (issue #58 Slice 3)
    raised `ModuleNotFoundError: No module named 'tests'` when run from the
    repo root (the canonical invocation) -- "tests" was never loaded at
    all, by anything, since nothing else ever imports it by that name.

    Registering `tests` here -- once, before any test module in this suite
    is collected, with `__path__` pointing at this directory -- makes it a
    real (if empty) package. Python's normal import machinery then resolves
    any `tests.<subpackage>.<module>` import (e.g. `tests.auth.mock_oidc_
    provider`) against the real files on disk, exactly as if `tests/
    __init__.py` existed, without adding that file: nothing on the
    filesystem changes, so this doesn't touch the INP001 convention, and it
    is local to `ps-service` (`ps-cli/tests` is untouched, so no risk of
    reintroducing issue #52's collision).
    """
    if "tests" in sys.modules:
        return
    module = types.ModuleType("tests")
    module.__path__ = [str(Path(__file__).parent)]
    sys.modules["tests"] = module


_register_tests_package_for_cross_package_imports()


@pytest.fixture(autouse=True)
def _isolate_logging(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Redirect `PS_LOGGING_DIR` to a per-test `tmp_path` and reset the facade around each test.

    Ensures no test ever reads leftover state from a previous test and no
    test call to `Logging.configure()`/`emit_log_entry` leaks into the real
    `logs/ps-service.jsonl`.

    Also guards against `litellm`'s import-time `dotenv.load_dotenv()`
    (triggered once any test imports `ps_service.llm_interface`) leaking the
    repo's real `.env` values for `PS_LLMINTERFACE_MODEL`/
    `PS_LLMINTERFACE_EMBED_MODEL` into `os.environ` for every subsequent
    test in the same pytest session.

    Also resets `ps_service.dependency_health`'s process-wide registry, the
    same leak-across-tests risk `reset_for_tests()` already guards against
    for Logging's own process-wide default emitter.
    """
    monkeypatch.setenv("PS_LOGGING_DIR", str(tmp_path))
    monkeypatch.delenv("PS_LLMINTERFACE_MODEL", raising=False)
    monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", raising=False)
    reset_for_tests()
    reset_dependency_health_for_tests()
    yield
    reset_for_tests()
    reset_dependency_health_for_tests()


@pytest.fixture
def make_emitter(tmp_path: Path):
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
def read_lines():
    """Read `log_path` (after a `.flush()`) and parse each line as JSON."""

    def _read(log_path: Path) -> list[dict[str, object]]:
        if not log_path.exists():
            return []
        return [
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line
        ]

    return _read
