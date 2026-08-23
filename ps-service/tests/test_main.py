"""Unit tests for the `ps_service.main` process harness (FastAPI app, liveness, readiness).

Uses `TestClient` in two distinct modes, per PLAN_REVIEWED.md §4:
- Bare `TestClient(app).get(...)`: never runs `lifespan` (no context manager
  entry), so it proves liveness/readiness behavior *before* startup completes.
- `with TestClient(app) as client:`: runs the full async `lifespan` startup
  (and shutdown, on exit) synchronously via Starlette's internal portal.
"""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import httpx
import ps_service.main as main_module
import pytest
from fastapi.testclient import TestClient
from ps_service.logging.errors import LoggingConfigurationError
from ps_service.logging.facade import reset_for_tests
from ps_service.main import app

_FORBIDDEN_IMPORT_PREFIXES = (
    "ps_service.api",
    "ps_service.ingestion",
    "ps_service.domain_mapper",
    "ps_service.company_merge",
    "ps_service.query_engine",
    "ps_service.mcp_interface",
    "ps_service.change_monitor",
    "ps_service.llm_interface",
)

_LEAK_SHAPED_KEYS = frozenset({"path", "config", "env", "traceback"})


def test_health_returns_200_and_alive_status_before_lifespan_runs() -> None:
    """GET /health via a bare (never-entered) TestClient returns 200 and 'alive'."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_ready_returns_200_and_not_ready_status_before_lifespan_runs() -> None:
    """GET /ready via a bare (never-entered) TestClient returns 200 and 'not_ready'."""
    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "not_ready"}


def test_ready_returns_ready_once_lifespan_startup_completes() -> None:
    """Entering TestClient as a context manager runs `lifespan` startup, flipping /ready to 'ready'."""
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_lifespan_calls_configure_before_emit_log_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-011: `configure()` is called before `emit_log_entry()` during lifespan startup.

    Patches `ps_service.main.configure`/`emit_log_entry` (where they are
    imported/used, not `ps_service.logging.facade` where they are defined),
    per the Logging facade's documented contract that `configure()` must run
    before any `emit_log_entry` call.
    """
    call_order: list[str] = []

    def fake_configure(*args: object, **kwargs: object) -> None:
        call_order.append("configure")

    def fake_emit_log_entry(*args: object, **kwargs: object) -> None:
        call_order.append("emit_log_entry")

    monkeypatch.setattr(main_module, "configure", fake_configure)
    monkeypatch.setattr(main_module, "emit_log_entry", fake_emit_log_entry)

    with TestClient(app):
        pass

    assert call_order == ["configure", "emit_log_entry"]


def test_lifespan_emits_exactly_one_startup_success_log_entry(tmp_path: Path) -> None:
    """AC-BI-012: exactly one structured log entry with action="startup"/outcome="success" is written.

    Uses the real Logging facade (no monkeypatching), writing to the
    `PS_LOGGING_DIR`-isolated `tmp_path` set up by the autouse conftest
    fixture. Critically, calls `reset_for_tests()` explicitly right after the
    `with` block exits and *before* reading the log file: `LogEmitter.emit()`
    only enqueues an entry, a background writer thread performs the actual
    file write, so reading the file without first draining and joining that
    thread would race and could observe an empty or partial file.
    """
    with TestClient(app):
        pass

    reset_for_tests()  # D1 fix: drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    startup_success_entries = [
        line for line in lines if line.get("action") == "startup" and line.get("outcome") == "success"
    ]

    assert len(startup_success_entries) == 1


@pytest.mark.parametrize("path", ["/health", "/ready"])
@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_non_get_request_to_health_or_ready_returns_405(path: str, method: str) -> None:
    """AC-BI-008: POST/PUT/DELETE to `/health` or `/ready` are rejected with 405.

    Likely passes with zero extra code (FastAPI/Starlette auto-405s a path
    registered only for GET) — written anyway as a locked-in regression guard.
    """
    response = getattr(TestClient(app), method)(path)

    assert response.status_code == 405


@pytest.mark.parametrize("path", ["/health", "/ready"])
@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_405_response_body_does_not_leak_path_config_env_or_traceback(path: str, method: str) -> None:
    """D5: the 405 body's key set must not contain any leak-shaped key.

    It need not equal `{"status"}` — Starlette's default 405 body
    `{"detail": "Method Not Allowed"}` is fine — it just must not leak
    anything unexpected (AC-BI-009).
    """
    response = getattr(TestClient(app), method)(path)

    assert response.json().keys().isdisjoint(_LEAK_SHAPED_KEYS)


def _get_bare_health() -> httpx.Response:
    """Helper: bare (never-entered) `TestClient` GET /health — lifespan never runs."""
    return TestClient(app).get("/health")


def _get_bare_ready() -> httpx.Response:
    """Helper: bare (never-entered) `TestClient` GET /ready — lifespan never runs."""
    return TestClient(app).get("/ready")


def _get_ready_after_lifespan_startup() -> httpx.Response:
    """Helper: GET /ready with `lifespan` startup run to completion."""
    with TestClient(app) as client:
        return client.get("/ready")


@pytest.mark.parametrize(
    "make_response",
    [_get_bare_health, _get_bare_ready, _get_ready_after_lifespan_startup],
)
def test_200_response_body_contains_only_a_status_key(make_response: Callable[[], httpx.Response]) -> None:
    """AC-BI-009 (final): every 200-response body's key set is exactly `{"status"}`.

    Covers every 200-response state reached by increments 1-4's tests: bare
    `/health`, bare `/ready`, and `/ready` after `lifespan` startup completes.
    """
    response = make_response()

    assert response.status_code == 200
    assert response.json().keys() == {"status"}


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_unauthenticated_get_never_returns_401_or_403(path: str) -> None:
    """AC-BI-001 (final, consolidated): unauthenticated GET to /health or /ready never returns 401/403.

    Explicit, dedicated test naming AC-BI-001 directly, per increment 12 —
    exercises both TestClient states (bare, and after lifespan startup
    completes) even though earlier increments' `==200` assertions already
    cover this incidentally.
    """
    bare_response = TestClient(app).get(path)
    assert bare_response.status_code not in (401, 403)

    with TestClient(app) as client:
        started_response = client.get(path)
    assert started_response.status_code not in (401, 403)


def test_main_module_does_not_statically_import_any_pipeline_or_query_surface_component() -> None:
    """AC-BI-006: `main.py` never imports `ps_service/api/` or any pipeline/query-surface component.

    Statically parses `main.py`'s source via `ast` and walks `Import`/
    `ImportFrom` nodes, rather than checking `sys.modules`, so it can't be
    fooled by conditional/lazy imports being missed by an import-based check
    (and also can't be fooled by something else having already imported one
    of these modules elsewhere, polluting `sys.modules`).
    """
    source = inspect.getsource(main_module)
    tree = ast.parse(source)

    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_names.append(node.module)
            imported_names.extend(f"{node.module}.{alias.name}" for alias in node.names)

    for name in imported_names:
        assert not name.startswith(_FORBIDDEN_IMPORT_PREFIXES), f"forbidden import found: {name}"


def test_lifespan_startup_failure_propagates_out_of_testclient_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-010 (unit half): a `configure()` failure is not swallowed by `lifespan`.

    Monkeypatches `configure` (as imported into `ps_service.main`) to raise
    `LoggingConfigurationError`, and asserts the exception propagates out of
    `with TestClient(app) as client: pass` rather than being caught anywhere
    along the way — proving startup failures fail fast (L1) instead of being
    silently absorbed.
    """

    def fake_configure(*args: object, **kwargs: object) -> None:
        raise LoggingConfigurationError("simulated log directory resolution failure")

    monkeypatch.setattr(main_module, "configure", fake_configure)

    with pytest.raises(LoggingConfigurationError), TestClient(app):
        pass


def test_main_calls_uvicorn_run_with_app_host_and_graceful_shutdown_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002/AC-BI-007 (config half): `main()` wires `uvicorn.run` with the harness's binding/shutdown config.

    Monkeypatches `uvicorn.run` (as imported into `ps_service.main`) with a
    `Mock` so no real bind happens, then asserts `main()` invokes it with the
    FastAPI `app` instance, `host="127.0.0.1"`, and
    `timeout_graceful_shutdown=10` — the *configuration* half of AC-BI-002
    (localhost-only default) and AC-BI-007 (explicit graceful-shutdown
    timeout, not left at uvicorn's default). The *behavioral* half (a real
    process actually binding to localhost and honoring the timeout on
    SIGTERM) is proven separately by increment 11's subprocess-based
    integration tests, not here.
    """
    mock_run = Mock()
    monkeypatch.setattr(main_module.uvicorn, "run", mock_run)

    main_module.main()

    mock_run.assert_called_once_with(
        app,
        host="127.0.0.1",
        port=8000,
        timeout_graceful_shutdown=10,
    )
