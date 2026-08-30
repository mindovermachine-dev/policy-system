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
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ps_service.main as main_module
from ps_service import dependency_health
from ps_service.config import ServiceConfig
from ps_service.ingestion.errors import IngestionConfigurationError
from ps_service.llm_interface import LlmProviderError
from ps_service.logging.errors import LoggingConfigurationError
from ps_service.logging.facade import reset_for_tests
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

_FORBIDDEN_IMPORT_PREFIXES = (
    "ps_service.domain_mapper",
    "ps_service.company_merge",
    "ps_service.query_engine",
    "ps_service.mcp_interface",
    "ps_service.change_monitor",
)

_LEAK_SHAPED_KEYS = frozenset({"path", "config", "env", "traceback"})


@pytest.fixture(autouse=True)
def _stub_dependency_checks_as_healthy(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default every dependency probe `_check_dependencies_at_startup` (issue #22) calls
    to succeed, so every pre-existing test below (written before real dependencies
    existed) keeps working without a real FalkorDB/LLM Provider/Cellar-ELI to talk to.

    Tests that specifically exercise the readiness-gating behavior override
    one of these via their own `monkeypatch` fixture argument — the same
    function-scoped `MonkeyPatch` instance as this fixture's, so a test's own
    `setattr` composes with (and can override) this default. A stub that
    succeeds never calls `mark_healthy`/`mark_unhealthy` itself, which is
    fine: `ps_service.dependency_health`'s registry already treats a
    never-recorded dependency as healthy by default, and `conftest.py`'s
    autouse fixture resets it before every test.
    """

    def stub_connect_from_config(config: ServiceConfig) -> object:
        """No-op FalkorDB connect: return an opaque handle standing in for a live `FalkorDB`."""
        return object()

    def stub_check_falkordb_connectivity(db: object, host: str, port: int) -> None:
        """No-op FalkorDB connectivity probe: a healthy dependency by default."""

    def stub_check_llm_interface_connectivity(config: ServiceConfig) -> None:
        """No-op LLM Interface connectivity probe: a healthy dependency by default."""

    def stub_check_cellar_eli_connectivity() -> None:
        """No-op Cellar/ELI connectivity probe: a healthy dependency by default."""

    monkeypatch.setattr(main_module, "connect_from_config", stub_connect_from_config)
    monkeypatch.setattr(
        main_module, "check_falkordb_connectivity", stub_check_falkordb_connectivity
    )
    monkeypatch.setattr(
        main_module, "check_llm_interface_connectivity", stub_check_llm_interface_connectivity
    )
    monkeypatch.setattr(
        main_module, "check_cellar_eli_connectivity", stub_check_cellar_eli_connectivity
    )


@pytest.fixture
def app() -> FastAPI:
    """Build a fresh app instance via `create_app`, per PLAN_REVIEWED.md §6's migration table.

    Uses the same host/port/timeout values #12 hardcoded, so migrated tests'
    expected behavior is unchanged; `logging_dir=None` preserves
    `conftest.py`'s existing `PS_LOGGING_DIR` isolation fixture behavior
    (falls back to `resolve_default_log_path()`, which reads the env var).
    """
    return create_app(
        ServiceConfig(
            host="127.0.0.1",
            port=8000,
            graceful_shutdown_seconds=10,
            logging_dir=None,
        )
    )


def test_health_returns_200_and_alive_status_before_lifespan_runs(app: FastAPI) -> None:
    """GET /health via a bare (never-entered) TestClient returns 200 and 'alive'."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_ready_returns_200_and_not_ready_status_before_lifespan_runs(app: FastAPI) -> None:
    """GET /ready via a bare (never-entered) TestClient returns 200 and 'not_ready'."""
    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "not_ready"}


def test_ready_returns_ready_once_lifespan_startup_completes(app: FastAPI) -> None:
    """Entering TestClient as a context manager runs `lifespan` startup, flipping
    /ready to 'ready'.
    """
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_lifespan_calls_configure_before_emit_log_entry(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
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


def test_lifespan_emits_exactly_one_startup_success_log_entry(tmp_path: Path, app: FastAPI) -> None:
    """AC-BI-012: exactly one structured log entry with
    action="startup"/outcome="success" is written.

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
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "success"
    ]

    assert len(startup_success_entries) == 1


@pytest.mark.parametrize("path", ["/health", "/ready"])
@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_non_get_request_to_health_or_ready_returns_405(
    path: str, method: str, app: FastAPI
) -> None:
    """AC-BI-008: POST/PUT/DELETE to `/health` or `/ready` are rejected with 405.

    Likely passes with zero extra code (FastAPI/Starlette auto-405s a path
    registered only for GET) — written anyway as a locked-in regression guard.
    """
    response = getattr(TestClient(app), method)(path)

    assert response.status_code == 405


@pytest.mark.parametrize("path", ["/health", "/ready"])
@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_405_response_body_does_not_leak_path_config_env_or_traceback(
    path: str, method: str, app: FastAPI
) -> None:
    """D5: the 405 body's key set must not contain any leak-shaped key.

    It need not equal `{"status"}` — Starlette's default 405 body
    `{"detail": "Method Not Allowed"}` is fine — it just must not leak
    anything unexpected (AC-BI-009).
    """
    response = getattr(TestClient(app), method)(path)

    assert response.json().keys().isdisjoint(_LEAK_SHAPED_KEYS)


def _get_bare_health(app: FastAPI) -> httpx.Response:
    """Helper: bare (never-entered) `TestClient` GET /health — lifespan never runs."""
    return TestClient(app).get("/health")  # pyright: ignore[reportReturnType]  # httpx double-build in dep tree (issue #48 R6): httpx2._models.Response vs httpx._models.Response


def _get_bare_ready(app: FastAPI) -> httpx.Response:
    """Helper: bare (never-entered) `TestClient` GET /ready — lifespan never runs."""
    return TestClient(app).get("/ready")  # pyright: ignore[reportReturnType]  # httpx double-build in dep tree (issue #48 R6): httpx2._models.Response vs httpx._models.Response


def _get_ready_after_lifespan_startup(app: FastAPI) -> httpx.Response:
    """Helper: GET /ready with `lifespan` startup run to completion."""
    with TestClient(app) as client:
        return client.get("/ready")  # pyright: ignore[reportReturnType]  # httpx double-build in dep tree (issue #48 R6): httpx2._models.Response vs httpx._models.Response


@pytest.mark.parametrize(
    "make_response",
    [_get_bare_health, _get_bare_ready, _get_ready_after_lifespan_startup],
)
def test_200_response_body_contains_only_a_status_key(
    make_response: Callable[[FastAPI], httpx.Response], app: FastAPI
) -> None:
    """AC-BI-009 (final): every 200-response body's key set is exactly `{"status"}`.

    Covers every 200-response state reached by increments 1-4's tests: bare
    `/health`, bare `/ready`, and `/ready` after `lifespan` startup completes.
    """
    response = make_response(app)

    assert response.status_code == 200
    assert response.json().keys() == {"status"}


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_unauthenticated_get_never_returns_401_or_403(path: str, app: FastAPI) -> None:
    """AC-BI-001 (final, consolidated): unauthenticated GET to /health or /ready
    never returns 401/403.

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
    """AC-BI-006 (narrowed by issues #22 and #51): `main.py` never imports a
    pipeline/query-surface component it doesn't need for its own contract.

    `ps_service.ingestion`/`ps_service.llm_interface` were dropped from
    `_FORBIDDEN_IMPORT_PREFIXES` by issue #22: `main.py` now imports each
    component's `check_connectivity` (and `ingestion.falkordb_client.
    connect_from_config`) as `/ready`'s startup dependency probes — a
    deliberate, narrow exception to AC-BI-006's original decoupling, not a
    reopening of it. Issue #51 drops `ps_service.api` for the same reason:
    `create_app` now mounts the `ps_service.api` REST router (a single
    top-level `from ps_service.api.routes import build_api_router`), exactly
    as #22 admitted `ingestion`/`llm_interface`. Domain Mapper, Company
    Merge, Query Engine, MCP Interface, and Regulatory Change Monitor stay
    forbidden — the AST scan is non-transitive (it parses `main.py`'s source
    only), and the pipeline stage entry points those routes eventually drive
    are imported lazily, function-local, never at `main.py` module load.

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


def test_lifespan_startup_failure_propagates_out_of_testclient_enter(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
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


def _delenv_all_ps_service_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every `PS_SERVICE_*` env var, giving `load_config()` a clean-env precondition."""
    for name in ("PS_SERVICE_HOST", "PS_SERVICE_PORT", "PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_main_calls_uvicorn_run_with_app_host_and_graceful_shutdown_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002/AC-BI-006/AC-BI-007: with no env overrides, `main()` wires
    `uvicorn.run` with the default config.

    Monkeypatches `uvicorn.run` (as imported into `ps_service.main`) with a
    `Mock` so no real bind happens, then asserts `main()` invokes it with a
    `FastAPI` app instance (identity equality against a fixture app is no
    longer possible by design, per AC-BI-008 — `main()` builds its own app
    internally via `create_app(load_config())`), `host="127.0.0.1"`,
    `port=8000`, and `timeout_graceful_shutdown=10` — the *configuration*
    half of AC-BI-002/006/007. The *behavioral* half (a real process actually
    binding to localhost and honoring the timeout on SIGTERM) is proven
    separately by the subprocess-based integration tests, not here.
    """
    _delenv_all_ps_service_vars(monkeypatch)
    mock_run = Mock()
    monkeypatch.setattr(main_module.uvicorn, "run", mock_run)

    main_module.main()

    mock_run.assert_called_once()
    call_args, call_kwargs = mock_run.call_args
    assert isinstance(call_args[0], FastAPI)
    assert call_kwargs == {
        "host": "127.0.0.1",
        "port": 8000,
        "timeout_graceful_shutdown": 10,
    }


def test_main_calls_load_config_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-002: `main()` resolves configuration via exactly one `load_config()` call.

    Wraps the real `load_config` in a `Mock(wraps=...)` spy so the actual
    resolution behavior is unchanged, mocks `uvicorn.run` so no real bind
    happens, then asserts the spy was invoked exactly once.
    """
    _delenv_all_ps_service_vars(monkeypatch)
    spy_load_config = Mock(wraps=main_module.load_config)
    monkeypatch.setattr(main_module, "load_config", spy_load_config)
    monkeypatch.setattr(main_module.uvicorn, "run", Mock())

    main_module.main()

    spy_load_config.assert_called_once()


@pytest.mark.parametrize(
    ("env_var", "env_value", "kwarg", "expected"),
    [
        ("PS_SERVICE_HOST", "0.0.0.0", "host", "0.0.0.0"),
        ("PS_SERVICE_PORT", "9090", "port", 9090),
        ("PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS", "30", "timeout_graceful_shutdown", 30),
    ],
)
def test_main_honors_ps_service_env_override_in_uvicorn_run_kwargs(
    env_var: str, env_value: str, kwarg: str, expected: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-003/004/005 (unit half): `main()` reflects a `PS_SERVICE_*` env
    override in `uvicorn.run`'s kwargs.

    Sets exactly one override env var at a time (the other two left cleared),
    mocks `uvicorn.run`, calls `main()`, and asserts the corresponding
    `uvicorn.run` kwarg reflects the overridden value end to end through
    `load_config()` -> `main()`.
    """
    _delenv_all_ps_service_vars(monkeypatch)
    monkeypatch.setenv(env_var, env_value)
    mock_run = Mock()
    monkeypatch.setattr(main_module.uvicorn, "run", mock_run)

    main_module.main()

    _, call_kwargs = mock_run.call_args
    assert call_kwargs[kwarg] == expected


def test_main_module_has_zero_os_environ_references() -> None:
    """AC-BI-012 (main.py half): `main.py` never references `os.environ`, not even via `.get(...)`.

    Stronger than `config.py`'s equivalent check (which permits
    `os.environ.get(...)`): `main.py` must route every env read through
    `ps_service.config.load_config()`, with zero direct `os.environ` access
    of any shape. Statically parses `main.py`'s source via `ast`, mirroring
    `test_main_module_does_not_statically_import_any_pipeline_or_query_surface_component`'s
    technique.
    """
    source = inspect.getsource(main_module)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            value = node.value
            is_os_environ = isinstance(value, ast.Name) and value.id == "os"
            assert not is_os_environ, "main.py must never reference os.environ directly"


def test_create_app_instances_have_independent_readiness_state() -> None:
    """AC-BI-008 (partial, readiness isolation only): two `create_app()` apps
    don't share `app.state.ready`.

    Constructs two independently-configured apps (configs are otherwise
    identical here — this test is scoped to readiness-flag isolation only,
    not config-content independence, which is a later increment's job) and
    enters only one's `TestClient` as a context manager (running its
    `lifespan` startup). The other app's `app.state.ready` must remain
    `False`, proving `app.state` is a genuine per-instance object rather
    than a shared module-level flag (the defect the old `_ready` module
    global had).
    """
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )
    started_app = create_app(config)
    untouched_app = create_app(config)

    with TestClient(started_app):
        pass

    assert untouched_app.state.ready is False


def test_lifespan_calls_configure_with_configs_logging_dir_joined_with_fixed_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-BI-007: `lifespan` calls `configure(log_path=config.logging_dir / "ps-service.jsonl")`.

    Constructs a `ServiceConfig` with a non-`None` `logging_dir` and
    monkeypatches `configure` to capture its call kwargs, proving the
    resolved config's `logging_dir` is threaded through explicitly rather
    than left to `configure()`'s own `resolve_default_log_path()` fallback.

    `config.logging_dir` is a *directory* (matching `PS_LOGGING_DIR`'s
    existing env-var semantics), while `configure(log_path=...)` treats a
    non-`None` `log_path` as a literal *file* path with no directory-to-file
    join of its own (that join only happens inside `resolve_default_log_path()`,
    which only runs when `log_path=None`). So `lifespan` must join
    `config.logging_dir` with the fixed filename `ps-service.jsonl` itself
    before calling `configure()` — passing the raw directory through would
    make the emitter's writer thread hit `IsADirectoryError` on every write,
    silently swallowed by the Logging facade's fallback-on-write-failure
    contract (AC#6 from issue #20), losing every log entry with no error
    surfaced anywhere.
    """
    captured_kwargs: dict[str, object] = {}

    def fake_configure(*args: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    def fake_emit_log_entry(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(main_module, "configure", fake_configure)
    monkeypatch.setattr(main_module, "emit_log_entry", fake_emit_log_entry)

    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=tmp_path,
    )
    scoped_app = create_app(config)

    with TestClient(scoped_app):
        pass

    assert captured_kwargs == {"log_path": tmp_path / "ps-service.jsonl"}


def test_create_app_instances_do_not_leak_each_others_logging_dir(tmp_path: Path) -> None:
    """AC-BI-008 (full): two `create_app()` calls with different `logging_dir`s
    stay independently configured.

    Distinct from `test_create_app_instances_have_independent_readiness_state`
    (which only proves `app.state.ready` isolation): this proves `config`
    *content* itself doesn't leak between `create_app()` instances, using
    REAL file-write assertions rather than kwarg-capture — now that the
    `logging_dir`/`log_path` join bug is fixed, this is safe (a prior version
    of this test used kwarg-capture specifically because that bug would have
    made real-file assertions fail against otherwise-correct code).

    Only the first app's `TestClient` is entered as a context manager, so
    only its `lifespan` runs; the second app's `lifespan` never runs at all.
    Uses the real Logging facade end to end (no monkeypatching), then, per
    `test_lifespan_emits_exactly_one_startup_success_log_entry`'s established
    pattern, calls `reset_for_tests()` right after the `with` block exits and
    *before* reading any file: `LogEmitter.emit()` only enqueues an entry, a
    background writer thread performs the actual file write, so reading
    without first draining and joining that thread would race.
    """
    first_logging_dir = tmp_path / "first"
    second_logging_dir = tmp_path / "second"

    first_config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=first_logging_dir,
    )
    second_config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=second_logging_dir,
    )
    first_app = create_app(first_config)
    second_app = create_app(second_config)

    with TestClient(first_app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    first_log_path = first_logging_dir / "ps-service.jsonl"
    lines = [
        json.loads(line) for line in first_log_path.read_text(encoding="utf-8").splitlines() if line
    ]
    startup_success_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "success"
    ]
    assert len(startup_success_entries) == 1

    # second app's lifespan never ran, so configure() was never called with its logging_dir; the
    # writer thread only mkdir()s the parent directory lazily on first write (emitter.py's
    # `_append_line`), so a directory that was never written to never gets created at all.
    assert not second_logging_dir.exists()
    assert second_app.state.ready is False


def test_lifespan_with_none_logging_dir_falls_back_to_resolve_default_log_path(
    tmp_path: Path,
) -> None:
    """Regression guard: `logging_dir=None` still resolves via
    `resolve_default_log_path()`, end to end.

    Guards against a regression where someone "fixes" the `None` branch too
    (e.g. always joining `_LOG_FILENAME`, even when `config.logging_dir` is
    `None`) and breaks the default path, which must keep delegating entirely
    to `resolve_default_log_path()`'s own directory+filename resolution
    (itself driven by `PS_LOGGING_DIR`, isolated to this same `tmp_path` by
    the autouse `_isolate_logging` conftest fixture — no separate `setenv`
    needed here since both fixtures resolve the identical per-test
    `tmp_path`). Uses the real Logging facade end to end and the same
    drain-before-read technique as the sibling real-file-write tests.
    """
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )
    scoped_app = create_app(config)

    with TestClient(scoped_app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    startup_success_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "success"
    ]
    assert len(startup_success_entries) == 1


def test_lifespan_emits_warning_log_entry_for_non_loopback_host(tmp_path: Path) -> None:
    """AC-BI-011: a non-loopback `config.host` (e.g. "0.0.0.0") produces exactly
    one warning log entry.

    The existing `outcome="success"` entry must still also be present (order:
    `configure()` succeeds, then the warning-if-non-loopback entry, then the
    existing success entry) — this proves the warning is additive, not a
    replacement. Uses the real Logging facade end to end and the same
    drain-before-read technique as the sibling real-file-write tests.
    """
    config = ServiceConfig(
        host="0.0.0.0",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )
    non_loopback_app = create_app(config)

    with TestClient(non_loopback_app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    warning_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "warning"
    ]
    success_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "success"
    ]

    assert len(warning_entries) == 1
    assert warning_entries[0].get("host") == "0.0.0.0"
    assert len(success_entries) == 1


def test_lifespan_emits_no_warning_log_entry_for_loopback_host(tmp_path: Path) -> None:
    """AC-BI-011 (negative case): a loopback `config.host` produces zero warning entries.

    Complements the already-existing `test_lifespan_emits_exactly_one_startup_success_log_entry`
    (which uses the default loopback host and asserts exactly one entry
    total) by making the "zero warnings for loopback" property explicit and
    independently checked.
    """
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
    )
    loopback_app = create_app(config)

    with TestClient(loopback_app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    warning_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "warning"
    ]
    success_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "success"
    ]

    assert len(warning_entries) == 0
    assert len(success_entries) == 1


# --- Dependency-gated readiness (issue #22) --------------------------------


def test_ready_stays_not_ready_after_startup_when_a_dependency_check_fails(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    def failing_connect(config: ServiceConfig) -> object:
        raise IngestionConfigurationError("FalkorDB connection failed at 127.0.0.1:6379")

    monkeypatch.setattr(main_module, "connect_from_config", failing_connect)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.json() == {"status": "not_ready"}


def test_startup_dependency_failure_emits_a_warning_log_entry_naming_the_dependency(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI, tmp_path: Path
) -> None:
    def failing_llm_check(config: ServiceConfig) -> None:
        raise LlmProviderError("PS_LLMINTERFACE_MODEL is not configured")

    monkeypatch.setattr(main_module, "check_llm_interface_connectivity", failing_llm_check)

    with TestClient(app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    warning_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "warning"
    ]

    assert any(entry.get("dependency") == "llm_interface" for entry in warning_entries)


def test_all_three_dependency_checks_run_even_when_the_first_one_fails(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """A FalkorDB failure must not short-circuit the LLM Interface/Cellar-ELI
    checks — a single startup should surface every failing dependency, not
    just the first one hit.
    """
    called: list[str] = []

    def failing_connect(config: ServiceConfig) -> object:
        called.append("falkordb")
        raise IngestionConfigurationError("boom")

    def succeeding_llm_check(config: ServiceConfig) -> None:
        called.append("llm_interface")

    def succeeding_cellar_check() -> None:
        called.append("cellar_eli")

    monkeypatch.setattr(main_module, "connect_from_config", failing_connect)
    monkeypatch.setattr(main_module, "check_llm_interface_connectivity", succeeding_llm_check)
    monkeypatch.setattr(main_module, "check_cellar_eli_connectivity", succeeding_cellar_check)

    with TestClient(app):
        pass

    assert called == ["falkordb", "llm_interface", "cellar_eli"]


def test_ready_flips_to_not_ready_when_a_dependency_is_marked_unhealthy_after_successful_startup(
    app: FastAPI,
) -> None:
    """The live gate (`dependency_health.all_healthy`), not just the one-time
    startup gate: a call site elsewhere (e.g. `graph_writer`'s write path)
    marking FalkorDB unhealthy mid-run must be reflected on the next
    `/ready` poll, without needing a restart.
    """
    with TestClient(app) as client:
        assert client.get("/ready").json() == {"status": "ready"}

        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=ConnectionError("boom"))

        assert client.get("/ready").json() == {"status": "not_ready"}


def test_ready_self_heals_once_the_unhealthy_dependency_recovers(app: FastAPI) -> None:
    with TestClient(app) as client:
        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=ConnectionError("boom"))
        assert client.get("/ready").json() == {"status": "not_ready"}

        dependency_health.mark_healthy(dependency_health.FALKORDB)

        assert client.get("/ready").json() == {"status": "ready"}
