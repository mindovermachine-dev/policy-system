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
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast
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
from ps_service.logging.facade import reset_for_tests, resolve_default_log_path
from ps_service.main import create_app
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.http_transport import MCP_HTTP_MOUNT_PATH

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    import httpx
    from starlette.applications import Starlette

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_FORBIDDEN_IMPORT_PREFIXES = (
    "ps_service.domain_mapper",
    "ps_service.company_merge",
    "ps_service.query_engine",
    "ps_service.change_monitor",
)

_JSON_RPC_ACCEPT = "application/json, text/event-stream"

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


def _complete_config(**overrides: object) -> ServiceConfig:
    """A `ServiceConfig` with every `INGESTION_REQUIRED_CONFIG_FIELDS` value set.

    The baseline for `app` below and any other fixture that needs `/ready`'s
    startup gate to be reachable — the readiness-gated-on-config-completeness
    tests further down build their own incomplete configs directly instead of
    using this helper.
    """
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "llm_interface_model": "azure/gpt-5.4-mini",
        "llm_interface_embed_model": "azure/text-embedding-3-small",
        "company_merge_similarity_threshold": 0.85,
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


@pytest.fixture
def app() -> FastAPI:
    """Build a fresh app instance via `create_app`, per PLAN_REVIEWED.md §6's migration table.

    Uses the same host/port/timeout values #12 hardcoded, so migrated tests'
    expected behavior is unchanged; `logging_dir=None` preserves
    `conftest.py`'s existing `PS_LOGGING_DIR` isolation fixture behavior
    (falls back to `resolve_default_log_path()`, which reads the env var).
    Ingestion-required config fields are all set (see `_complete_config`) so
    pre-existing tests, written before issue #16's follow-up gated `/ready`
    on config completeness too, keep passing unchanged.
    """
    return create_app(_complete_config())


def test_health_returns_200_and_alive_status_before_lifespan_runs(app: FastAPI) -> None:
    """GET /health via a bare (never-entered) TestClient returns 200 and 'alive'."""
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_ready_returns_200_and_not_ready_status_before_lifespan_runs(app: FastAPI) -> None:
    """GET /ready via a bare (never-entered) TestClient returns 200 and 'not_ready'."""
    response = TestClient(app).get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "not_ready", "unhealthy_dependencies": []}


def test_ready_returns_ready_once_lifespan_startup_completes(app: FastAPI) -> None:
    """Entering TestClient as a context manager runs `lifespan` startup, flipping
    /ready to 'ready'.
    """
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "unhealthy_dependencies": []}


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
    ("make_response", "expected_keys"),
    [
        (_get_bare_health, {"status"}),
        (_get_bare_ready, {"status", "unhealthy_dependencies"}),
        (_get_ready_after_lifespan_startup, {"status", "unhealthy_dependencies"}),
    ],
)
def test_200_response_body_contains_only_a_status_key(
    make_response: Callable[[FastAPI], httpx.Response],
    expected_keys: set[str],
    app: FastAPI,
) -> None:
    """AC-BI-009 (final): every 200-response body's key set is exactly the documented shape.

    Covers every 200-response state reached by increments 1-4's tests: bare
    `/health` (`{"status"}`), and bare/post-`lifespan`-startup `/ready`
    (`{"status", "unhealthy_dependencies"}` since issue #68) — no undocumented
    key ever leaks into either response.
    """
    response = make_response(app)

    assert response.status_code == 200
    assert response.json().keys() == expected_keys


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
    as #22 admitted `ingestion`/`llm_interface`. Issue #39 drops
    `ps_service.mcp_interface` for the same reason: `create_app` mounts the
    MCP Interface's Streamable HTTP transport
    (`http_transport.build_streamable_http_app`), exactly as #51 admitted
    `ps_service.api`. Domain Mapper, Company Merge, Query Engine, and
    Regulatory Change Monitor stay forbidden — the AST scan is non-transitive
    (it parses `main.py`'s source only), and the pipeline stage entry points
    those routes eventually drive are imported lazily, function-local, never
    at `main.py` module load.

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


def test_main_does_not_call_uvicorn_run_when_bypass_refuses_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002 (issue #67): `main()`'s refusal happens strictly before `uvicorn.run`.

    Mirrors `test_main_calls_uvicorn_run_with_app_host_and_graceful_shutdown_timeout`,
    but with the bypass active and a non-loopback host: `main()` must raise
    `LocalTestBypassBindRefusedError` and never reach `uvicorn.run` at all.
    """
    _delenv_all_ps_service_vars(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    monkeypatch.setenv("PS_SERVICE_HOST", "0.0.0.0")
    mock_run = Mock()
    monkeypatch.setattr(main_module.uvicorn, "run", mock_run)

    with pytest.raises(main_module.LocalTestBypassBindRefusedError):
        main_module.main()

    mock_run.assert_not_called()


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
    config = _complete_config(host="0.0.0.0")
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
    config = _complete_config()
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


def test_lifespan_refuses_when_bypass_active_and_host_not_loopback() -> None:
    """AC-BI-002/AC-BI-003 (issue #67): bypass active + non-loopback host refuses to bind.

    `lifespan()` startup must raise `LocalTestBypassBindRefusedError` before
    any port is bound, and the message must name both facts an operator needs
    to fix this immediately: the bypass being active, and the configured host
    not being loopback (AC-BI-003).
    """
    config = _complete_config(host="0.0.0.0", is_local_test_bypass_active=True)
    app = create_app(config)

    with (
        pytest.raises(main_module.LocalTestBypassBindRefusedError) as excinfo,
        TestClient(app),
    ):
        pass

    message = str(excinfo.value)
    assert "local-test bypass" in message
    assert "active" in message
    assert "0.0.0.0" in message
    assert "loopback" in message


def test_lifespan_refuses_before_mcp_session_manager_starts_when_bypass_active_and_host_not_loopback(  # noqa: E501 - name mirrors PLAN.md Slice 5 verbatim
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-004, extended to the new transport (issue #39, PLAN.md Slice 5): the
    refusal at bypass-active + non-loopback host must fire *before* the MCP
    Streamable HTTP session manager's `lifespan_context` is ever entered, not just
    "probably does because it's earlier in the function" (already covered in
    general by `test_lifespan_refuses_when_bypass_active_and_host_not_loopback`
    above).

    `mcp_asgi_app` is a local variable inside `create_app`, never returned or
    exposed on `app.state`. Per PLAN.md Slice 5's test-authoring note, the seam
    is `main_module.build_streamable_http_app` itself (as imported into
    `ps_service.main`): this wraps it so the real sub-app it returns has its
    `router.lifespan_context` replaced with a recording stand-in *before*
    `create_app` uses it -- mirroring the existing
    `monkeypatch.setattr(main_module, "connect_from_config", ...)` pattern
    already used throughout this file. No production code change expected: if
    this fails, Slice 4's statement order in `main.py` is wrong, not this test.
    """
    entered = False
    real_build_streamable_http_app = main_module.build_streamable_http_app

    def wrapped_build_streamable_http_app(*, host: str) -> Starlette:
        mcp_asgi_app = real_build_streamable_http_app(host=host)

        @asynccontextmanager
        async def recording_lifespan_context(app: object) -> AsyncGenerator[None]:
            nonlocal entered
            entered = True
            yield

        mcp_asgi_app.router.lifespan_context = recording_lifespan_context
        return mcp_asgi_app

    monkeypatch.setattr(main_module, "build_streamable_http_app", wrapped_build_streamable_http_app)

    config = _complete_config(host="0.0.0.0", is_local_test_bypass_active=True)
    app = create_app(config)

    with pytest.raises(main_module.LocalTestBypassBindRefusedError), TestClient(app):
        pass

    assert entered is False


def test_lifespan_does_not_refuse_when_bypass_active_and_host_is_loopback() -> None:
    """AC-BI-002 (negative case, issue #67): bypass active + loopback host starts normally."""
    config = _complete_config(is_local_test_bypass_active=True)
    app = create_app(config)

    with TestClient(app):
        pass


def test_lifespan_still_only_warns_when_bypass_inactive_and_host_not_loopback(
    tmp_path: Path,
) -> None:
    """AC-BI-004 (regression, issue #67): bypass inactive (default) + non-loopback host
    is still warning-only, never a refusal.

    Reuses `test_lifespan_emits_warning_log_entry_for_non_loopback_host`'s
    read-log-lines technique to prove that test's assertions still hold after
    this slice's refusal check was added — not merely that they held before
    it.
    """
    config = _complete_config(host="0.0.0.0")
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

    assert len(warning_entries) == 1
    assert warning_entries[0].get("host") == "0.0.0.0"


def test_lifespan_emits_bypass_warning_entry_when_active(tmp_path: Path) -> None:
    """AC-BI-007 (issue #67): bypass active (+ loopback host) emits exactly one
    startup warning entry stating both facts an operator needs — the bypass is
    active, and the guarantee is loopback-only — additive to (not replacing)
    the unconditional `outcome="success"` entry.
    """
    config = _complete_config(is_local_test_bypass_active=True)
    app = create_app(config)

    with TestClient(app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    bypass_warning_entries = [line for line in lines if "local_test_bypass_active" in line]
    success_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "success"
    ]

    assert len(bypass_warning_entries) == 1
    entry = bypass_warning_entries[0]
    assert entry.get("action") == "startup"
    assert entry.get("outcome") == "warning"
    assert entry.get("local_test_bypass_active") is True
    assert entry.get("bind_scope") == "loopback-only"
    assert len(success_entries) == 1


def test_lifespan_emits_no_bypass_warning_entry_when_inactive(tmp_path: Path) -> None:
    """AC-BI-007 (negative case, issue #67): bypass inactive (the default) emits
    zero bypass-warning entries — the warning is conditional on the bypass
    actually being active, not unconditional startup noise.
    """
    config = _complete_config()
    app = create_app(config)

    with TestClient(app):
        pass

    reset_for_tests()

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    bypass_warning_entries = [line for line in lines if "local_test_bypass_active" in line]

    assert len(bypass_warning_entries) == 0


def test_lifespan_emits_bypass_warning_on_every_start_not_only_first(tmp_path: Path) -> None:
    """AC-BI-007 (issue #67): every start, not only the first, gets its own warning.

    Two separate process starts (two independent `create_app` + `TestClient`
    entries, sharing this test's `tmp_path`-backed log sink) each emit their
    own bypass-warning entry; nothing dedups or gates on prior-warning state.
    """
    config = _complete_config(is_local_test_bypass_active=True)

    with TestClient(create_app(config)):
        pass
    with TestClient(create_app(config)):
        pass

    reset_for_tests()

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    bypass_warning_entries = [line for line in lines if "local_test_bypass_active" in line]

    assert len(bypass_warning_entries) == 2


def test_lifespan_emits_bypass_warning_entry_every_start_with_mcp_transport_mounted(
    tmp_path: Path,
) -> None:
    """AC-BI-005, extended to the new transport (issue #39, PLAN.md Slice 6): the
    every-start bypass-warning regression guard proven above by
    `test_lifespan_emits_bypass_warning_on_every_start_not_only_first` (#67) still
    holds now that `lifespan` wraps its tail in
    `async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):` (Slice 4) --
    proving that nesting did not accidentally move the warning emission to only
    fire once, or suppress it, now that it sits one level deeper relative to
    `yield`.

    CHANGES.md's F-1 correction applies: this calls `create_app(config)` TWICE,
    once per `with TestClient(...)` block, producing two independent apps (two
    distinct `mcp_asgi_app`/`StreamableHTTPSessionManager` instances) -- entering
    one `app`'s session manager twice via two separate `TestClient` blocks would
    raise `RuntimeError` (`StreamableHTTPSessionManager.run()` is one-shot per
    the installed SDK), unrelated to `main.py`'s statement order. This exactly
    mirrors the #67 precedent test's own two-independent-`create_app()`-calls
    shape.
    """
    config = _complete_config(is_local_test_bypass_active=True)

    with TestClient(create_app(config)):
        pass
    with TestClient(create_app(config)):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    bypass_warning_entries = [line for line in lines if "local_test_bypass_active" in line]

    assert len(bypass_warning_entries) == 2


# --- Dependency-gated readiness (issue #22) --------------------------------


def test_ready_stays_not_ready_after_startup_when_a_dependency_check_fails(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    def failing_connect(config: ServiceConfig) -> object:
        raise IngestionConfigurationError("FalkorDB connection failed at 127.0.0.1:6379")

    monkeypatch.setattr(main_module, "connect_from_config", failing_connect)

    with TestClient(app) as client:
        response = client.get("/ready")

    # `failing_connect` raises inside `connect_from_config`, before
    # `check_falkordb_connectivity` (the call that would `mark_unhealthy`) is
    # ever reached — so the live registry never records FalkorDB as unhealthy,
    # even though `app.state.ready` correctly stays False. An empty list here
    # is the textually correct response (AC-BI-001 says "currently recorded"
    # unhealthy), not a bug.
    assert response.json() == {"status": "not_ready", "unhealthy_dependencies": []}


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
    """The live gate (`dependency_health`'s registry, read via `is_healthy`),
    not just the one-time startup gate: a call site elsewhere (e.g.
    `graph_writer`'s write path) marking FalkorDB unhealthy mid-run must be
    reflected on the next `/ready` poll, without needing a restart.
    """
    with TestClient(app) as client:
        assert client.get("/ready").json() == {"status": "ready", "unhealthy_dependencies": []}

        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=ConnectionError("boom"))

        assert client.get("/ready").json() == {
            "status": "not_ready",
            "unhealthy_dependencies": ["falkordb"],
        }


def test_ready_self_heals_once_the_unhealthy_dependency_recovers(app: FastAPI) -> None:
    with TestClient(app) as client:
        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=ConnectionError("boom"))
        assert client.get("/ready").json() == {
            "status": "not_ready",
            "unhealthy_dependencies": ["falkordb"],
        }

        dependency_health.mark_healthy(dependency_health.FALKORDB)

        assert client.get("/ready").json() == {"status": "ready", "unhealthy_dependencies": []}


def test_ready_response_has_empty_unhealthy_dependencies_list_when_ready(app: FastAPI) -> None:
    """AC-BI-002: a fully healthy `/ready` response names zero unhealthy dependencies."""
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.json() == {"status": "ready", "unhealthy_dependencies": []}


def test_ready_response_lists_unhealthy_dependency_names_when_not_ready(app: FastAPI) -> None:
    """AC-BI-001: an unhealthy dependency's name appears in `/ready`'s response."""
    with TestClient(app) as client:
        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=ConnectionError("boom"))

        response = client.get("/ready")

    assert response.json() == {"status": "not_ready", "unhealthy_dependencies": ["falkordb"]}


def test_ready_response_never_contains_the_raw_mark_unhealthy_error_string(app: FastAPI) -> None:
    """AC-BI-003: `mark_unhealthy`'s raw error string never leaks into `/ready`'s
    response — only the dependency's name does.
    """
    with TestClient(app) as client:
        dependency_health.mark_unhealthy(
            dependency_health.FALKORDB,
            error=RuntimeError("super-secret-connection-string-should-never-leak"),
        )

        response = client.get("/ready")

    assert "super-secret-connection-string-should-never-leak" not in response.text
    assert response.json()["unhealthy_dependencies"] == ["falkordb"]


# --- Config-completeness-gated readiness (issue #16 follow-up) -------------


def test_ready_stays_not_ready_after_startup_when_ingestion_config_is_incomplete() -> None:
    """A missing `INGESTION_REQUIRED_CONFIG_FIELDS` value keeps `/ready` at
    `not_ready` even though every dependency probe succeeds — the same
    outcome a caller previously only discovered by getting a 503 from
    `POST /ingestions`.
    """
    incomplete_app = create_app(_complete_config(company_merge_similarity_threshold=None))

    with TestClient(incomplete_app) as client:
        response = client.get("/ready")

    assert response.json() == {"status": "not_ready", "unhealthy_dependencies": []}


def test_ready_returns_ready_when_ingestion_config_is_complete() -> None:
    """Regression guard: a fully-set config still reaches `ready` (proves the
    new gate doesn't regress the already-passing case, independent of the
    `app` fixture's own default).
    """
    complete_app = create_app(_complete_config())

    with TestClient(complete_app) as client:
        response = client.get("/ready")

    assert response.json() == {"status": "ready", "unhealthy_dependencies": []}


def test_startup_config_incompleteness_emits_a_warning_log_entry_naming_missing_fields(
    tmp_path: Path,
) -> None:
    incomplete_app = create_app(
        _complete_config(llm_interface_model=None, company_merge_similarity_threshold=None)
    )

    with TestClient(incomplete_app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    warning_entries = [
        line
        for line in lines
        if line.get("action") == "startup" and line.get("outcome") == "warning"
    ]
    missing_config_entries = [
        entry.get("missing_config") for entry in warning_entries if "missing_config" in entry
    ]

    assert missing_config_entries == [["llm_interface_model", "company_merge_similarity_threshold"]]


def test_ready_never_self_heals_missing_config_without_a_restart(app: FastAPI) -> None:
    """Config completeness has no live gate (unlike dependency reachability):
    it is a frozen `ServiceConfig` value, so nothing during the process's
    life can ever make a missing field appear — proving there is no
    equivalent of `dependency_health.mark_healthy` for this gate.
    """
    incomplete_app = create_app(_complete_config(company_merge_similarity_threshold=None))

    with TestClient(incomplete_app) as client:
        assert client.get("/ready").json() == {"status": "not_ready", "unhealthy_dependencies": []}
        assert client.get("/ready").json() == {"status": "not_ready", "unhealthy_dependencies": []}


# --- MCP Streamable HTTP transport mounted at the composition root (issue #39) ---


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally (mirrors
    `mcp_interface/test_cypher_tool.py`'s fake, not imported across files, per
    `test_main_integration.py`'s own self-contained-fakes convention).
    """

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally: `query()` always returns the scripted result."""

    def __init__(self, *, result: _FakeQueryResult) -> None:
        self._result = result

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        return self._result


class _FakeFalkorDB:
    """Stands in for the eager `falkordb.FalkorDB` client."""

    def __init__(self, handle: _FakeGraphHandle) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> _FakeGraphHandle:
        return self._handle


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an already-`isinstance`-checked JSON value to `dict[str, object]`."""
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _as_list(value: object) -> list[object]:
    """Narrow an already-`isinstance`-checked JSON value to `list[object]`."""
    assert isinstance(value, list)
    return cast("list[object]", value)


def _sse_result(response_text: str) -> dict[str, object]:
    """Extract the JSON-RPC `result` object from an SSE-formatted response body."""
    for line in response_text.splitlines():
        if line.startswith("data:"):
            payload: object = json.loads(line.removeprefix("data:").strip())
            payload_dict = _as_dict(payload)
            return _as_dict(payload_dict["result"])
    pytest.fail(f"no 'data:' line found in SSE body: {response_text!r}")


def _initialize_mcp_session(client: TestClient) -> str:
    """Drive `initialize` -> `notifications/initialized` against the mounted transport,
    returning the session id.
    """
    response = client.post(
        f"{MCP_HTTP_MOUNT_PATH}/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-main-client", "version": "0.0.1"},
            },
        },
        headers={"Accept": _JSON_RPC_ACCEPT},
    )
    assert response.status_code == 200
    session_id = response.headers["mcp-session-id"]

    notified = client.post(
        f"{MCP_HTTP_MOUNT_PATH}/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Accept": _JSON_RPC_ACCEPT, "mcp-session-id": session_id},
    )
    assert notified.status_code == 202
    return session_id


def test_create_app_mounts_mcp_streamable_http_transport_at_fixed_path() -> None:
    """AC-BI-001/002: `create_app` mounts MCP Interface's Streamable HTTP transport
    at `MCP_HTTP_MOUNT_PATH`, reachable through the same `app`/`TestClient` that
    already serves `/health` in this file.

    A real JSON-RPC `initialize` request over the ASGI transport (real HTTP
    verbs/headers/JSON-RPC, not an in-process function call) succeeding proves
    the transport is wired through the real composition root, not just the
    standalone factory already proven by `tests/mcp_interface/test_http_transport.py`
    (Slice 3).
    """
    app = create_app(_complete_config())

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-main-client", "version": "0.0.1"},
                },
            },
            headers={"Accept": _JSON_RPC_ACCEPT},
        )

    assert response.status_code == 200
    result = _sse_result(response.text)
    server_info = _as_dict(result["serverInfo"])
    assert isinstance(server_info.get("name"), str)


def test_cypher_and_domain_concepts_both_reachable_via_mounted_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-BI-003 (through the real composition root): both the `cypher` tool and
    the `psdomain://concepts` resource are reachable over the mounted transport
    in the same session — composing the fact in one test, not two disconnected
    ones (mirrors #67's own established convention).
    """
    fake_graph = _FakeGraphHandle(result=_FakeQueryResult(header=[[0, "id"]], result_set=[["a"]]))

    def _stub_mcp_connect_from_config(_config: object) -> _FakeFalkorDB:
        return _FakeFalkorDB(fake_graph)

    monkeypatch.setattr(mcp_server, "connect_from_config", _stub_mcp_connect_from_config)

    md_file = tmp_path / "ps-domain-concepts.md"
    md_file.write_text("# PS domain concepts\n\nRegulation -> Obligation\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_domain_concepts_path", lambda: md_file)

    app = create_app(_complete_config())

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        session_id = _initialize_mcp_session(client)

        cypher_response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "cypher", "arguments": {"query": "MATCH (n) RETURN n.id"}},
            },
            headers={"Accept": _JSON_RPC_ACCEPT, "mcp-session-id": session_id},
        )
        resource_response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": "psdomain://concepts"},
            },
            headers={"Accept": _JSON_RPC_ACCEPT, "mcp-session-id": session_id},
        )

    assert cypher_response.status_code == 200
    cypher_result = _sse_result(cypher_response.text)
    assert cypher_result["isError"] is False

    assert resource_response.status_code == 200
    resource_result = _sse_result(resource_response.text)
    contents = _as_list(resource_result["contents"])
    first = _as_dict(contents[0])
    assert first["text"] == md_file.read_text(encoding="utf-8")


def test_query_executed_over_mcp_http_transport_with_bypass_active_carries_fixed_local_principal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, read_lines: ReadLines
) -> None:
    """AC-BI-006 (issue #39, PLAN.md Slice 7 -- the flagship test of the whole
    issue): composes AC-BI-001/002/003/006 into one end-to-end scenario.

    Extends `test_main_integration.py`'s own (#67)
    `test_local_test_bypass_active_on_loopback_starts_and_answers_query_without_credential`
    -- which drives `mcp_server.cypher()` in-process, i.e. as the stdio
    transport would -- to the new mounted Streamable HTTP transport
    specifically: the *same* fake-FalkorDB shape and the *same* final
    principal assertion, but the tool call itself now goes through a real
    JSON-RPC `initialize` -> `notifications/initialized` -> `tools/call`
    exchange over the ASGI transport (`_initialize_mcp_session`, already
    used by `test_cypher_and_domain_concepts_both_reachable_via_mounted_transport`
    above), with no header/token/credential of any kind beyond the mandatory
    `mcp-session-id` the protocol itself requires.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")

    fake_graph = _FakeGraphHandle(result=_FakeQueryResult(header=[[0, "id"]], result_set=[["a"]]))

    def _stub_mcp_connect_from_config(_config: object) -> _FakeFalkorDB:
        return _FakeFalkorDB(fake_graph)

    monkeypatch.setattr(mcp_server, "connect_from_config", _stub_mcp_connect_from_config)

    config = _complete_config(is_local_test_bypass_active=True, logging_dir=tmp_path)
    app = create_app(config)

    with TestClient(app, base_url=f"http://{config.host}:{config.port}") as client:
        session_id = _initialize_mcp_session(client)

        response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "cypher", "arguments": {"query": "MATCH (n) RETURN n.id"}},
            },
            headers={"Accept": _JSON_RPC_ACCEPT, "mcp-session-id": session_id},
        )

    assert response.status_code == 200
    result = _sse_result(response.text)
    assert result["isError"] is False

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading
    lines = read_lines(resolve_default_log_path())
    entry = next(
        line
        for line in lines
        if line.get("component") == "query_engine" and line.get("action") == "execute_cypher_query"
    )
    assert entry["principal"] == mcp_server.LOCAL_TEST_PRINCIPAL_ID
