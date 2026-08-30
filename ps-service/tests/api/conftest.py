"""Shared fixtures for ``ps_service.api`` tests.

``make_emitter``/``read_lines`` continue to resolve from the root
``tests/conftest.py`` (pytest picks the nearer conftest first, then falls back);
this module adds the API-specific ``app_config`` and ``client`` fixtures per
PLAN_REVIEWED.md §1.1 m8. The root autouse ``_isolate_logging`` fixture still
applies.

``client`` is a *bare* ``TestClient`` — its ``lifespan`` is deliberately not
entered. Increment 1's only endpoint (``GET /regulations``) emits no log lines
and needs no readiness state, so running ``lifespan`` would only add real
dependency probes and leave the process-wide Logging facade configured (which
``reset_for_tests`` does not fully undo — the ``atexit`` guard persists). A
later increment that needs ``configure()`` to have run will layer a
lifespan-entered client (with stubbed dependency probes) on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from ps_service.config import ServiceConfig
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def configured_logging(tmp_path: Path) -> Iterator[Path]:
    """Install a real process-wide Logging facade for one test, fully restored afterwards.

    Increment 7's run-context test proves the async ``provide_run_id`` binding
    reaches the log lines the ingestion orchestration emits during a request
    through the *process-default* emitter (the ``POST /ingestions`` route passes
    no explicit ``emitter``), so it needs a real ``configure()``d facade rather
    than a stubbed ``emit_log_entry``.

    ``reset_for_tests()`` nulls the default emitter but deliberately leaves the
    module-global ``_atexit_registered`` guard set. ``tests/api`` collects before
    ``tests/logging``, so this fixture also saves and restores that flag --
    otherwise a real ``configure()`` here would poison
    ``tests/logging/test_facade_emit_log_entry.py``'s
    ``test_atexit_drain_hook_registered_once_when_configure_called_multiple_times``,
    which asserts ``atexit.register`` runs exactly once from a clean start.

    Yields:
        The path the default emitter writes to. Drain the writer thread with
        ``ps_service.logging.facade.reset_for_tests()`` before reading it.
    """
    log_path = tmp_path / "run-context.jsonl"
    # _atexit_registered is a module global that reset_for_tests() does not clear;
    # capture it so a real configure() here cannot leak into tests/logging's
    # once-only atexit assertion.
    saved_atexit_registered = (
        facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    )
    facade.configure(log_path=log_path)
    try:
        yield log_path
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = (  # pyright: ignore[reportPrivateUsage]
            saved_atexit_registered
        )


@pytest.fixture
def app_config() -> ServiceConfig:
    """A loopback ``ServiceConfig`` for building a test app (``logging_dir=None``)."""
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )


@pytest.fixture
def client(app_config: ServiceConfig) -> TestClient:
    """A bare ``TestClient`` over an app built from ``app_config`` (``lifespan`` not entered)."""
    return TestClient(create_app(app_config))
