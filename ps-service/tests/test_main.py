"""Unit tests for the `ps_service.main` process harness (FastAPI app, liveness, readiness).

Uses `TestClient` in two distinct modes, per PLAN_REVIEWED.md §4:
- Bare `TestClient(app).get(...)`: never runs `lifespan` (no context manager
  entry), so it proves liveness/readiness behavior *before* startup completes.
- `with TestClient(app) as client:`: runs the full async `lifespan` startup
  (and shutdown, on exit) synchronously via Starlette's internal portal.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import tomllib
from contextlib import asynccontextmanager
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ps_service.main as main_module
from ps_service import dependency_health
from ps_service.config import ServiceConfig, load_config
from ps_service.ingestion.errors import IngestionConfigurationError
from ps_service.llm_interface import LlmProviderError
from ps_service.logging.errors import LoggingConfigurationError
from ps_service.logging.facade import reset_for_tests, resolve_default_log_path
from ps_service.main import create_app
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.http_transport import MCP_HTTP_MOUNT_PATH
from ps_service.passkey_signing.store import (
    connect_from_config as connect_passkey_signing_postgres_from_config,
)
from ps_service.persistence import (
    check_connectivity_from_config as check_state_postgres_connectivity,
)
from ps_service.persistence import connect_from_config as connect_state_postgres_from_config
from ps_test_support.required_startup_env import REQUIRED_STARTUP_ENV

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    import httpx
    from fastapi.responses import JSONResponse
    from fastapi.routing import APIRoute
    from starlette.applications import Starlette

    from ps_service.auth.models import AuthContext
    from ps_service.auth.verifier import PsTokenVerifier

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

    `check_state_postgres_connectivity` (issue #133) is stubbed here too --
    unlike `check_passkey_signing_postgres_connectivity` (a genuine no-op
    when unconfigured, so it never needed a stub), PS state Postgres's own
    connectivity check deliberately raises even when unconfigured (PLAN.md
    §0.11's fail-closed divergence from Passkey Signing's own precedent), so
    without this stub every test in this file that runs `lifespan` startup
    would pick up a spurious `state_postgres` entry in `unhealthy_dependencies`
    and an extra startup warning log line, purely from `_complete_config()`
    leaving `state_postgres_host` at its `None` default -- unrelated to
    whatever readiness/logging behavior each test actually exercises.
    """

    def stub_check_falkordb_connectivity(config: ServiceConfig) -> None:
        """No-op FalkorDB connect-and-check: a healthy dependency by default."""

    def stub_check_llm_interface_connectivity(config: ServiceConfig) -> None:
        """No-op LLM Interface connectivity probe: a healthy dependency by default."""

    def stub_check_cellar_eli_connectivity() -> None:
        """No-op Cellar/ELI connectivity probe: a healthy dependency by default."""

    def stub_check_state_postgres_connectivity(config: ServiceConfig) -> None:
        """No-op PS state Postgres connectivity probe: a healthy dependency by default."""

    monkeypatch.setattr(
        main_module, "check_falkordb_connectivity", stub_check_falkordb_connectivity
    )
    monkeypatch.setattr(
        main_module, "check_llm_interface_connectivity", stub_check_llm_interface_connectivity
    )
    monkeypatch.setattr(
        main_module, "check_cellar_eli_connectivity", stub_check_cellar_eli_connectivity
    )
    monkeypatch.setattr(
        main_module, "check_state_postgres_connectivity", stub_check_state_postgres_connectivity
    )


def _fake_fetch_discovery_document(issuer: str, **_kwargs: object) -> dict[str, Any]:
    """A successful discovery document, exactly matching `_stub_resolve_auth_context`'s
    old hardcoded `AuthContext` fields (`jwks_uri`, `RS256`-only algorithm support), so every
    pre-existing assertion in this file keeps passing unchanged.

    Mirrors `tests/invitations/test_startup.py`'s/`tests/authz/
    test_bootstrap_owner_startup_fail_closed.py`'s own helper of the same shape.
    """
    return {
        "jwks_uri": f"{issuer}/jwks.json",
        "id_token_signing_alg_values_supported": ["RS256"],
    }


@pytest.fixture(autouse=True)
def _stub_auth_discovery(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Autouse: exercise the real `resolve_auth_context`, faking only the one real network
    boundary beneath it (`fetch_discovery_document`, already an approved boundary target).

    This file exercises the process harness (logging, readiness, the #67
    local-test bypass, uvicorn wiring) -- none of its tests are about OIDC
    discovery itself, which is `tests/auth/test_startup_fail_closed.py`'s
    job. Without this stub, `_complete_config()`'s fake
    `https://issuer.example.com` pair (and `_delenv_all_ps_service_vars`'s
    equivalent env vars) would make `create_app` attempt a real,
    doomed-to-fail network fetch for nearly every test in this file. Unlike
    the previous `_stub_resolve_auth_context` (a hand-rolled reimplementation
    of `resolve_auth_context`'s own bypass/presence/discovery decision
    logic one layer above the true boundary), this fakes only
    `fetch_discovery_document` -- `resolve_auth_context` itself, including its
    bypass check, missing-config error, and discovery-document validation, now
    runs for real on every test in this file.
    """
    monkeypatch.setattr(
        "ps_service.auth.startup.fetch_discovery_document", _fake_fetch_discovery_document
    )


def _complete_config(**overrides: object) -> ServiceConfig:
    """A `ServiceConfig` with every `INGESTION_REQUIRED_CONFIG_FIELDS` value set.

    The baseline for `app` below and any other fixture that needs `/ready`'s
    startup gate to be reachable — the readiness-gated-on-config-completeness
    tests further down build their own incomplete configs directly instead of
    using this helper. `auth_issuer`/`auth_audience` default to a fake
    placeholder pair (issue #58): `create_app` now fails closed
    (`AuthConfigurationError`) unless the local-test bypass is active or both
    are set, and this helper's own callers have nothing to do with auth or
    the bypass -- defaulting the auth pair here (rather than forcing the
    bypass on) deliberately leaves `is_local_test_bypass_active` at its own
    `False` default, so this file's many bypass-semantics tests (which build
    `ServiceConfig`/call `_complete_config` expecting bypass-inactive
    behavior) are undisturbed. A test exercising the new fail-closed
    behavior overrides the auth pair directly via
    `_complete_config(auth_issuer=None, auth_audience=None)`.

    Same rationale applies to `authz_bootstrap_owner_subject`/`_issuer`
    (issue #144): `create_app` now also fails closed
    (`AccessRoleBootstrapConfigurationError`) unless the local-test bypass is
    active or both are set, so a fake placeholder pair is defaulted here too.

    `authentik_api_token`/`authentik_base_url` (issue #140) are also
    defaulted here -- unlike the pairs above, `create_app`'s check for these
    (`require_authentik_credential_configured`) is unconditional, with no
    local-test-bypass exemption, so every test funnelling through this
    helper needs both set regardless of `is_local_test_bypass_active`.
    """
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "llm_interface_model": "azure/gpt-5.4-mini",
        "llm_interface_embed_model": "azure/text-embedding-3-small",
        "company_merge_similarity_threshold": 0.85,
        "auth_issuer": "https://issuer.example.com",
        "auth_audience": "https://api.example.com",
        "authz_bootstrap_owner_subject": "first-owner-subject",
        "authz_bootstrap_owner_issuer": "https://issuer.example.com",
        "authentik_api_token": "test-authentik-token",
        "authentik_base_url": "https://authentik.example.com",
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
    assert response.json()["status"] == "alive"


def test_health_returns_version_field_from_installed_metadata(app: FastAPI) -> None:
    """AC-BI-001: `/health`'s `version` field comes from `installed_version("ps-service")`.

    Calls the real `importlib.metadata.version("ps-service")` directly and compares --
    safe to run for real (no mocking needed): the very next test,
    `test_health_version_matches_ps_service_pyproject_toml_version`, already proves this same
    real call's value is not a coincidental hardcoded literal, since it independently tracks
    `pyproject.toml`'s own declared version.
    """
    response = TestClient(app).get("/health")

    assert response.json() == {
        "status": "alive",
        "version": installed_version("ps-service"),
    }


def test_health_version_matches_ps_service_pyproject_toml_version(app: FastAPI) -> None:
    """AC-BI-002 (dev-tree-real half): `/health`'s reported version matches
    `ps-service/pyproject.toml`'s own declared `[project] version`.

    No monkeypatching -- reads `pyproject.toml` directly via `tomllib.load(...)`, proving the
    real, currently-installed distribution metadata (`importlib.metadata.version("ps-service")`)
    agrees with the source tree's own declared version.
    """
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        pyproject_data = tomllib.load(pyproject_file)
    expected_version = pyproject_data["project"]["version"]

    response = TestClient(app).get("/health")

    assert response.json()["version"] == expected_version


def test_ready_returns_503_and_not_ready_status_before_lifespan_runs(app: FastAPI) -> None:
    """GET /ready via a bare (never-entered) TestClient returns 503 and 'not_ready'."""
    response = TestClient(app).get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "unhealthy_dependencies": []}


def test_ready_returns_ready_once_lifespan_startup_completes(app: FastAPI) -> None:
    """Entering TestClient as a context manager runs `lifespan` startup, flipping
    /ready to 'ready'.
    """
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "unhealthy_dependencies": []}


def test_lifespan_calls_configure_before_emit_log_entry(tmp_path: Path, app: FastAPI) -> None:
    """AC-BI-011: `configure()` runs before any `emit_log_entry()` call during lifespan startup.

    Exercises the real Logging facade (no monkeypatching): `emit_log_entry`'s own real
    contract raises `LoggingLifecycleError` if no default emitter has been installed yet, so
    `lifespan` completing without that error, *and* leaving behind a real written log entry,
    is itself proof `configure()` already ran before `emit_log_entry` was first called --
    state/output, not a call-order interaction list.
    """
    with TestClient(app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]

    assert len(lines) >= 1


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
    ("make_response", "expected_status", "expected_keys"),
    [
        (_get_bare_health, 200, {"status", "version"}),
        (_get_bare_ready, 503, {"status", "unhealthy_dependencies"}),
        (_get_ready_after_lifespan_startup, 200, {"status", "unhealthy_dependencies"}),
    ],
)
def test_response_body_contains_only_a_status_key(
    make_response: Callable[[FastAPI], httpx.Response],
    expected_status: int,
    expected_keys: set[str],
    app: FastAPI,
) -> None:
    """AC-BI-009 (final): every response body's key set is exactly the documented shape.

    Covers every state reached by increments 1-4's tests: bare `/health`
    (200, `{"status"}`), bare `/ready` before `lifespan` runs (503, not
    ready), and `/ready` after `lifespan` startup completes (200, ready) —
    both `/ready` states carry `{"status", "unhealthy_dependencies"}` (issue
    #68) — no undocumented key ever leaks into either response.
    """
    response = make_response(app)

    assert response.status_code == expected_status
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
    component's `check_connectivity` (`ingestion.falkordb_client.
    check_connectivity_from_config` for FalkorDB) as `/ready`'s startup
    dependency probes — a
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app: FastAPI
) -> None:
    """AC-BI-010 (unit half): a `configure()` failure is not swallowed by `lifespan`.

    Forces the real `configure()` to fail for real, no monkeypatching of `configure` itself:
    redirects `PS_LOGGING_DIR` to a path that already exists as a regular file, so
    `resolve_default_log_path()`'s own `log_dir.mkdir(...)` call raises a real `OSError`,
    which `configure()` re-raises as `LoggingConfigurationError` -- then asserts that real
    exception propagates out of `with TestClient(app): pass` rather than being caught
    anywhere along the way, proving startup failures fail fast (L1) instead of being
    silently absorbed.
    """
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_text("occupies the path configure() will try to mkdir", encoding="utf-8")
    monkeypatch.setenv("PS_LOGGING_DIR", str(blocked_path))

    with pytest.raises(LoggingConfigurationError), TestClient(app):
        pass


def _delenv_all_ps_service_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every `PS_SERVICE_*` env var, giving `load_config()` a clean-env precondition.

    Also sets a fake `PS_AUTH_ISSUER`/`PS_AUTH_AUDIENCE` pair (issue #58), a
    fake `PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT`/`PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER`
    pair (issue #144), and `REQUIRED_STARTUP_ENV` (currently
    `PS_AUTHENTIK_API_TOKEN`/`PS_AUTHENTIK_BASE_URL`, issue #140): these tests call `main()`
    end to end (real `load_config()`, not `_complete_config()`), have nothing to do with auth,
    RBAC bootstrap, or Authentik, and don't set the local-test bypass --
    without fake values, `create_app` would now fail closed
    (`AuthConfigurationError`, then `AccessRoleBootstrapConfigurationError`,
    then `AuthentikCredentialConfigurationError` -- the last one
    unconditionally, bypass or not). Kept deliberately independent of the
    bypass (never set here) so a host override to a non-loopback address
    (see the parametrized test below) never collides with
    `_refuse_non_loopback_bypass_bind`'s bypass-active-only guard.
    """
    for name in ("PS_SERVICE_HOST", "PS_SERVICE_PORT", "PS_SERVICE_GRACEFUL_SHUTDOWN_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PS_AUTH_ISSUER", "https://issuer.example.com")
    monkeypatch.setenv("PS_AUTH_AUDIENCE", "https://api.example.com")
    monkeypatch.setenv("PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT", "first-owner-subject")
    monkeypatch.setenv("PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER", "https://issuer.example.com")
    for key, value in REQUIRED_STARTUP_ENV.items():
        monkeypatch.setenv(key, value)


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
    # detroit-exception: binding a real socket is unsafe here; call args ARE the spec (§1.2)
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
    # A `Mock(wraps=...)` spy delegates to the real `load_config` (actual resolution behavior
    # unchanged) -- only the call *count* below is asserted, and that count is the literally
    # specified behavior here (§1.2), not a stand-in for business logic.
    # detroit-exception: spy-through wrapping the real collaborator; call count IS the spec (§1.2)
    spy_load_config = Mock(wraps=main_module.load_config)
    # detroit-exception: spy-through wrapping the real collaborator; call count IS the spec (§1.2)
    monkeypatch.setattr(main_module, "load_config", spy_load_config)
    # detroit-exception: starting a real ASGI server is unsafe/expensive in a unit test (§1.2)
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
    # detroit-exception: binding a real socket is unsafe here; call args ARE the spec (§1.2)
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
    # detroit-exception: binding a real socket is unsafe here; the non-call IS the spec (§1.2)
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
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )
    started_app = create_app(config)
    untouched_app = create_app(config)

    with TestClient(started_app):
        pass

    assert untouched_app.state.ready is False


def test_lifespan_calls_configure_with_configs_logging_dir_joined_with_fixed_filename(
    tmp_path: Path,
) -> None:
    """AC-BI-007: `lifespan` calls `configure(log_path=config.logging_dir / "ps-service.jsonl")`.

    Exercises the real Logging facade (no monkeypatching): constructs a `ServiceConfig` with a
    non-`None` `logging_dir`, then asserts a real log entry lands at exactly
    `config.logging_dir / "ps-service.jsonl"` -- state/output, rather than capturing
    `configure()`'s call kwargs via a stub.

    `config.logging_dir` is a *directory* (matching `PS_LOGGING_DIR`'s
    existing env-var semantics), while `configure(log_path=...)` treats a
    non-`None` `log_path` as a literal *file* path with no directory-to-file
    join of its own (that join only happens inside `resolve_default_log_path()`,
    which only runs when `log_path=None`). So `lifespan` must join
    `config.logging_dir` with the fixed filename `ps-service.jsonl` itself
    before calling `configure()` -- passing the raw directory through would
    make the emitter's writer thread hit `IsADirectoryError` on every write,
    silently swallowed by the Logging facade's fallback-on-write-failure
    contract (AC#6 from issue #20): no file would ever land at this exact
    path, and the real-state assertion below would fail for exactly that
    reason.
    """
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=tmp_path,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )
    scoped_app = create_app(config)

    with TestClient(scoped_app):
        pass

    reset_for_tests()  # drain the emitter's queue and join its writer thread before reading

    log_path = tmp_path / "ps-service.jsonl"
    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]

    assert len(lines) >= 1


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
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )
    second_config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=second_logging_dir,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
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
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
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
    `monkeypatch.setattr(main_module, "check_falkordb_connectivity", ...)`
    pattern already used throughout this file. No production code change expected: if
    this fails, Slice 4's statement order in `main.py` is wrong, not this test.
    """
    entered = False
    real_build_streamable_http_app = main_module.build_streamable_http_app

    def wrapped_build_streamable_http_app(
        *, host: str, verifier: PsTokenVerifier | None, auth_context: AuthContext | None
    ) -> Starlette:
        mcp_asgi_app = real_build_streamable_http_app(
            host=host, verifier=verifier, auth_context=auth_context
        )

        @asynccontextmanager
        async def recording_lifespan_context(app: object) -> AsyncGenerator[None]:
            nonlocal entered
            entered = True
            yield

        mcp_asgi_app.router.lifespan_context = recording_lifespan_context
        return mcp_asgi_app

    # detroit-exception: wraps/delegates to the real fn, only the lifespan hook swaps (§2 case 9)
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
    def failing_falkordb_check(config: ServiceConfig) -> None:
        error = IngestionConfigurationError("FalkorDB connection failed at 127.0.0.1:6379")
        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=error)
        raise error

    monkeypatch.setattr(main_module, "check_falkordb_connectivity", failing_falkordb_check)

    with TestClient(app) as client:
        response = client.get("/ready")

    # Regression for a real bug found via manual end-to-end testing (issue #68):
    # `check_connectivity_from_config` wraps both the connect step and the
    # `list_graphs()` probe in one `dependency_health`-recording try/except, so
    # a failure anywhere in there now reliably lands FalkorDB in
    # `unhealthy_dependencies`, not just an empty list alongside a correct but
    # unhelpful "not_ready" status.
    assert response.json() == {"status": "not_ready", "unhealthy_dependencies": ["falkordb"]}


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

    def failing_falkordb_check(config: ServiceConfig) -> None:
        called.append("falkordb")
        raise IngestionConfigurationError("boom")

    def succeeding_llm_check(config: ServiceConfig) -> None:
        called.append("llm_interface")

    def succeeding_cellar_check() -> None:
        called.append("cellar_eli")

    # detroit-exception: fixed-order/non-short-circuiting across all 3 probes IS the spec here
    monkeypatch.setattr(main_module, "check_falkordb_connectivity", failing_falkordb_check)
    # detroit-exception: fixed-order/non-short-circuiting across all 3 probes IS the spec here
    monkeypatch.setattr(main_module, "check_llm_interface_connectivity", succeeding_llm_check)
    # detroit-exception: fixed-order/non-short-circuiting across all 3 probes IS the spec here
    monkeypatch.setattr(main_module, "check_cellar_eli_connectivity", succeeding_cellar_check)

    with TestClient(app):
        pass

    assert called == ["falkordb", "llm_interface", "cellar_eli"]


# --- Passkey Signing Postgres readiness (issue #131, PLAN.md §0.8) ---------


def test_passkey_signing_postgres_is_ready_but_not_gating_dependency() -> None:
    """An outage of this optional, pilot-scope Postgres instance must never
    block `/ready` for the rest of the system (PLAN.md §0.8) -- it is
    tracked (`_READY_DEPENDENCIES`) but never gates (`_GATING_DEPENDENCIES`),
    unlike FalkorDB.

    Reads both private module constants via `getattr` by name (rather than
    `main_module._READY_DEPENDENCIES` attribute-access syntax), mirroring
    this file's own `monkeypatch.setattr(main_module, "_GATING_DEPENDENCIES",
    ...)` precedent for touching these same private names without an
    `reportPrivateUsage` suppression.
    """
    ready_dependencies = cast(
        "tuple[str, ...]",
        getattr(main_module, "_READY_DEPENDENCIES"),  # noqa: B009 - see docstring
    )
    gating_dependencies = cast(
        "tuple[str, ...]",
        getattr(main_module, "_GATING_DEPENDENCIES"),  # noqa: B009 - see docstring
    )

    assert dependency_health.PASSKEY_SIGNING_POSTGRES in ready_dependencies
    assert dependency_health.PASSKEY_SIGNING_POSTGRES not in gating_dependencies


def test_ready_stays_ready_when_only_passkey_signing_postgres_check_fails(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """A failed connection is reflected as unhealthy without ever raising out
    of `/ready`/crashing startup (PLAN.md §0.8): `_check_dependencies_at_startup`
    catches the probe's own exception, records it via `dependency_health`, and
    `status` stays `"ready"` since this dependency is deliberately excluded
    from `_GATING_DEPENDENCIES` -- unlike an equivalent FalkorDB failure,
    which does flip `status` to `"not_ready"` (see the FalkorDB-equivalent
    test above).
    """

    def failing_passkey_signing_postgres_check(config: ServiceConfig) -> None:
        error = ConnectionError("Passkey Signing Postgres connection failed")
        dependency_health.mark_unhealthy(dependency_health.PASSKEY_SIGNING_POSTGRES, error=error)
        raise error

    monkeypatch.setattr(
        main_module,
        "check_passkey_signing_postgres_connectivity",
        failing_passkey_signing_postgres_check,
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.json() == {
        "status": "ready",
        "unhealthy_dependencies": ["passkey_signing_postgres"],
    }


def test_migration_runner_is_skipped_at_startup_when_postgres_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """PLAN.md §0.6/§0.8: an environment that never configures Passkey Signing
    Postgres must not even attempt a connection at startup -- `app`'s
    `_complete_config()` leaves `passkey_signing_postgres_host` at its `None`
    default.
    """

    def fail_if_called(config: ServiceConfig) -> object:
        message = "connect_from_config must not be called when Postgres is unconfigured"
        raise AssertionError(message)

    # detroit-exception: raise-if-called trap proving a never-called ordering guarantee (§2 case 3)
    monkeypatch.setattr(main_module, "connect_passkey_signing_postgres_from_config", fail_if_called)

    with TestClient(app):
        pass


@pytest.mark.postgres_live
def test_migration_runner_runs_at_startup_when_postgres_is_configured() -> None:
    """PLAN.md §0.6: startup applies pending migrations once via the injected connection.

    `postgres_live`-marked (mirrors `tests/passkey_signing/test_migration_runner.py`'s own
    convention -- there is no meaningful fake for "did this SQL actually apply"): runs the real
    `create_app`/`lifespan` startup path against a real, reachable Passkey Signing Postgres
    instance, then asserts on real applied-migration state (a real `schema_migrations` row)
    afterwards, rather than an `applied_with` interaction-count list on a faked
    `apply_pending_migrations` -- proving startup actually wires the real connection through to
    the real migration runner, not just that some callable was invoked once.
    """
    real_config = load_config()
    assert real_config.passkey_signing_postgres_host is not None, (
        "postgres_live requires PS_PASSKEYSIGNING_POSTGRES_HOST to be set"
    )
    config = _complete_config(
        passkey_signing_postgres_host=real_config.passkey_signing_postgres_host,
        passkey_signing_postgres_port=real_config.passkey_signing_postgres_port,
        passkey_signing_postgres_database=real_config.passkey_signing_postgres_database,
        passkey_signing_postgres_user=real_config.passkey_signing_postgres_user,
        passkey_signing_postgres_password=real_config.passkey_signing_postgres_password,
    )

    with TestClient(create_app(config)):
        pass

    with connect_passkey_signing_postgres_from_config(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM schema_migrations WHERE filename = %(filename)s",
            {"filename": "0001_pending_approvals.sql"},
        )
        recorded = cur.fetchone() is not None

    assert recorded is True


# --- PS state Postgres readiness and fatal migration failure (issue #130, CHANGES F1) ---


def test_state_postgres_is_ready_and_gating_dependency() -> None:
    """Unlike Passkey Signing Postgres, PS state Postgres backs privileged state (roles, audit,
    runtime config, the catalog override): without it the service cannot serve, so it gates
    `/ready` (issue #130) -- a first `helm install` must not report ready with no usable store.
    """
    ready_dependencies = cast(
        "tuple[str, ...]",
        getattr(main_module, "_READY_DEPENDENCIES"),  # noqa: B009 - see the passkey twin above
    )
    gating_dependencies = cast(
        "tuple[str, ...]",
        getattr(main_module, "_GATING_DEPENDENCIES"),  # noqa: B009 - see the passkey twin above
    )

    assert dependency_health.STATE_POSTGRES in ready_dependencies
    assert dependency_health.STATE_POSTGRES in gating_dependencies


def test_ready_is_not_ready_when_state_postgres_check_fails(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    def failing_state_postgres_check(config: ServiceConfig) -> None:
        error = ConnectionError("PS state Postgres connection failed")
        dependency_health.mark_unhealthy(dependency_health.STATE_POSTGRES, error=error)
        raise error

    monkeypatch.setattr(
        main_module, "check_state_postgres_connectivity", failing_state_postgres_check
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "unhealthy_dependencies": ["state_postgres"],
    }


def test_startup_raises_when_state_migrations_cannot_be_applied_and_host_is_configured() -> None:
    """A configured-but-unusable state store is fatal at startup (the process exits and the
    orchestrator restarts it) rather than a warning: a service that cannot migrate its own
    schema must never come up looking healthy.
    """
    config = _complete_config(
        state_postgres_host="127.0.0.1",
        state_postgres_port=59999,
        state_postgres_database="ps_state",
        state_postgres_user="ps_state",
        state_postgres_password="unused",
    )

    with pytest.raises(psycopg.Error), TestClient(create_app(config)):
        pass


def test_startup_skips_state_migrations_when_host_is_unset_and_ready_reports_not_ready(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """Host unset: no connection is even attempted at startup and the process stays up, but
    `/ready` is `not_ready` via the (real) connectivity probe, which raises when unconfigured.
    """

    def fail_if_called(config: ServiceConfig) -> object:
        message = "connect_from_config must not be called when the state Postgres is unconfigured"
        raise AssertionError(message)

    # detroit-exception: raise-if-called trap proving a never-called ordering guarantee (§2 case 3)
    monkeypatch.setattr(main_module, "connect_state_postgres_from_config", fail_if_called)
    # The autouse stub reports every dependency healthy; restore the real probe for this test.
    monkeypatch.setattr(
        main_module, "check_state_postgres_connectivity", check_state_postgres_connectivity
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "unhealthy_dependencies": ["state_postgres"],
    }


@pytest.mark.postgres_live
def test_state_migrations_run_at_startup_apply_every_component_baseline() -> None:
    """Startup wires the real PS state connection to the runner for all three components.

    `postgres_live`-marked, like the Passkey Signing twin above: runs the real
    `create_app`/`lifespan` path against a reachable PS state Postgres, then reads back the
    real `ps_schema_migrations` tracking rows (issue #130: audit, access roles, runtime config).
    """
    real_config = load_config()
    assert real_config.state_postgres_host is not None, (
        "postgres_live requires PS_STATE_POSTGRES_HOST to be set"
    )
    config = _complete_config(
        state_postgres_host=real_config.state_postgres_host,
        state_postgres_port=real_config.state_postgres_port,
        state_postgres_database=real_config.state_postgres_database,
        state_postgres_user=real_config.state_postgres_user,
        state_postgres_password=real_config.state_postgres_password,
    )

    with TestClient(create_app(config)):
        pass

    with connect_state_postgres_from_config(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT component, filename FROM ps_schema_migrations")
        tracked = {(row[0], row[1]) for row in cur.fetchall()}
    assert {
        ("audit", "0001_audit_events.sql"),
        ("authz", "0001_access_role_assignments.sql"),
        ("runtime_config", "0001_runtime_config.sql"),
        ("ingestion_runs", "0001_ingestion_runs.sql"),
    } <= tracked


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


def test_ready_stays_ready_when_llm_interface_is_marked_unhealthy_live(app: FastAPI) -> None:
    """AC-BI-001/AC-BI-005 (live half): only FalkorDB health determines `/ready`'s
    status — an LLM Interface outage recorded on the live registry still names
    it in `unhealthy_dependencies` but must not flip `status` to `not_ready`.
    """
    with TestClient(app) as client:
        assert client.get("/ready").json() == {"status": "ready", "unhealthy_dependencies": []}

        dependency_health.mark_unhealthy(
            dependency_health.LLM_INTERFACE, error=ConnectionError("boom")
        )

        assert client.get("/ready").json() == {
            "status": "ready",
            "unhealthy_dependencies": ["llm_interface"],
        }


def test_ready_stays_ready_when_cellar_eli_is_marked_unhealthy_live(app: FastAPI) -> None:
    """AC-BI-001/AC-BI-006 (live half): a Cellar/ELI outage recorded on the live
    registry still names it in `unhealthy_dependencies` but must not flip
    `status` to `not_ready`.
    """
    with TestClient(app) as client:
        assert client.get("/ready").json() == {"status": "ready", "unhealthy_dependencies": []}

        dependency_health.mark_unhealthy(
            dependency_health.CELLAR_ELI, error=ConnectionError("boom")
        )

        assert client.get("/ready").json() == {
            "status": "ready",
            "unhealthy_dependencies": ["cellar_eli"],
        }


def test_ready_self_heals_llm_interface_name_from_unhealthy_dependencies_without_restart(
    app: FastAPI,
) -> None:
    """AC-BI-004 (extended to LLM Interface): once LLM Interface recovers, its
    name drops from `unhealthy_dependencies` on the next poll, without a
    restart. `status` stays `ready` throughout, since LLM Interface never
    gates it.
    """
    with TestClient(app) as client:
        dependency_health.mark_unhealthy(
            dependency_health.LLM_INTERFACE, error=ConnectionError("boom")
        )
        assert client.get("/ready").json() == {
            "status": "ready",
            "unhealthy_dependencies": ["llm_interface"],
        }

        dependency_health.mark_healthy(dependency_health.LLM_INTERFACE)

        assert client.get("/ready").json() == {"status": "ready", "unhealthy_dependencies": []}


def test_ready_self_heals_cellar_eli_name_from_unhealthy_dependencies_without_restart(
    app: FastAPI,
) -> None:
    """AC-BI-004 (extended to Cellar/ELI): once Cellar/ELI recovers, its name
    drops from `unhealthy_dependencies` on the next poll, without a restart.
    `status` stays `ready` throughout, since Cellar/ELI never gates it.
    """
    with TestClient(app) as client:
        dependency_health.mark_unhealthy(
            dependency_health.CELLAR_ELI, error=ConnectionError("boom")
        )
        assert client.get("/ready").json() == {
            "status": "ready",
            "unhealthy_dependencies": ["cellar_eli"],
        }

        dependency_health.mark_healthy(dependency_health.CELLAR_ELI)

        assert client.get("/ready").json() == {"status": "ready", "unhealthy_dependencies": []}


def test_ready_lists_both_llm_interface_and_cellar_eli_when_both_unhealthy_but_stays_ready(
    app: FastAPI,
) -> None:
    """Strengthens AC-BI-001/AC-BI-003 against a "only one dependency at a time"
    blind spot: both non-FalkorDB dependencies unhealthy at once are both
    named, in `_READY_DEPENDENCIES`'s declared order, while `status` stays
    `ready` since FalkorDB itself is untouched.
    """
    with TestClient(app) as client:
        dependency_health.mark_unhealthy(
            dependency_health.LLM_INTERFACE, error=ConnectionError("boom")
        )
        dependency_health.mark_unhealthy(
            dependency_health.CELLAR_ELI, error=ConnectionError("boom")
        )

        response = client.get("/ready")

    assert response.json() == {
        "status": "ready",
        "unhealthy_dependencies": ["llm_interface", "cellar_eli"],
    }


def test_ready_is_ready_when_llm_interface_startup_probe_fails_but_falkordb_succeeds(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """AC-BI-002/AC-BI-005 (startup half): an LLM Interface probe failure at
    startup must not wedge `app.state.ready` -- only a FalkorDB startup
    failure does. Once `lifespan` completes, `/ready` reports ready, still
    naming LLM Interface as unhealthy.
    """

    def failing_llm_check(config: ServiceConfig) -> None:
        error = LlmProviderError("PS_LLMINTERFACE_MODEL is not configured")
        dependency_health.mark_unhealthy(dependency_health.LLM_INTERFACE, error=error)
        raise error

    monkeypatch.setattr(main_module, "check_llm_interface_connectivity", failing_llm_check)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.json() == {"status": "ready", "unhealthy_dependencies": ["llm_interface"]}


def test_ready_is_ready_when_cellar_eli_startup_probe_fails_but_falkordb_succeeds(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """AC-BI-002/AC-BI-006 (startup half): a Cellar/ELI probe failure at
    startup must not wedge `app.state.ready` -- only a FalkorDB startup
    failure does. Once `lifespan` completes, `/ready` reports ready, still
    naming Cellar/ELI as unhealthy.
    """

    def failing_cellar_check() -> None:
        error = IngestionConfigurationError("Cellar/ELI connection failed")
        dependency_health.mark_unhealthy(dependency_health.CELLAR_ELI, error=error)
        raise error

    monkeypatch.setattr(main_module, "check_cellar_eli_connectivity", failing_cellar_check)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.json() == {"status": "ready", "unhealthy_dependencies": ["cellar_eli"]}


def test_startup_cellar_eli_failure_emits_a_warning_log_entry_naming_the_dependency(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI, tmp_path: Path
) -> None:
    """AC-BI-014 (extended to Cellar/ELI): mirrors
    `test_startup_dependency_failure_emits_a_warning_log_entry_naming_the_dependency`
    (LLM Interface) -- a Cellar/ELI startup probe failure must still emit a
    warning log entry naming it, even though it no longer affects
    `app.state.ready`.
    """

    def failing_cellar_check() -> None:
        raise IngestionConfigurationError("Cellar/ELI connection failed")

    monkeypatch.setattr(main_module, "check_cellar_eli_connectivity", failing_cellar_check)

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

    assert any(entry.get("dependency") == "cellar_eli" for entry in warning_entries)


# --- /ready retries a still-latched gating dependency instead of restarting (issue #124) ---


def test_ready_recovers_once_falkordb_becomes_reachable_after_a_failed_startup_probe(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """AC-BI-002: the exact race `spikes/deploy-ps-azure/README.md` documented --
    FalkorDB unreachable at ps-service startup, then reachable seconds later.
    Before issue #124, `app.state.ready` latched `False` forever once the
    startup probe failed; now the next `/ready` poll after recovery reports
    `ready`, with no process restart.
    """
    falkordb_reachable = False

    def flaky_falkordb_check(config: ServiceConfig) -> None:
        if falkordb_reachable:
            dependency_health.mark_healthy(dependency_health.FALKORDB)
            return
        error = IngestionConfigurationError("FalkorDB connection failed at 127.0.0.1:6379")
        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=error)
        raise error

    monkeypatch.setattr(main_module, "check_falkordb_connectivity", flaky_falkordb_check)

    with TestClient(app) as client:
        assert client.get("/ready").json() == {
            "status": "not_ready",
            "unhealthy_dependencies": ["falkordb"],
        }

        falkordb_reachable = True

        assert client.get("/ready").json() == {"status": "ready", "unhealthy_dependencies": []}


def test_ready_keeps_reporting_not_ready_on_repeated_polls_while_falkordb_stays_down(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """AC-BI-008: a retried gating-dependency probe failing again must not
    raise out of the request handler -- polling `/ready` repeatedly while
    FalkorDB is still down keeps answering `503 not_ready`, poll after poll.
    """

    def failing_falkordb_check(config: ServiceConfig) -> None:
        error = IngestionConfigurationError("FalkorDB connection failed at 127.0.0.1:6379")
        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=error)
        raise error

    monkeypatch.setattr(main_module, "check_falkordb_connectivity", failing_falkordb_check)

    with TestClient(app) as client:
        for _ in range(3):
            response = client.get("/ready")
            assert response.status_code == 503
            assert response.json() == {
                "status": "not_ready",
                "unhealthy_dependencies": ["falkordb"],
            }


def test_ready_flips_ready_only_once_every_member_of_an_extended_gating_set_succeeds(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """AC-BI-005: the retry loop is written against `_GATING_DEPENDENCIES`
    (today: FalkorDB alone), not hardcoded to FalkorDB by name -- proven here by widening the
    *real* `_GATING_DEPENDENCIES` set to also cover LLM Interface: an already-real dependency
    `_all_dependency_probes` already probes for, not a fabricated identity standing in for a
    future one. `_all_dependency_probes` itself is never replaced -- the real (name, probe)
    wiring for both dependencies runs unmodified, via the same already-approved-boundary
    `check_falkordb_connectivity`/`check_llm_interface_connectivity` substitution pattern this
    file uses throughout -- so this proves the retry loop's own iteration is generic, not that a
    synthetic probe list happens to behave correctly. `app.state.ready` must flip `True` only
    once every gating dependency succeeds, and each is retried independently on later polls.
    """
    falkordb_reachable = False
    llm_interface_reachable = False

    def flaky_falkordb_probe(config: ServiceConfig) -> None:
        del config
        if falkordb_reachable:
            dependency_health.mark_healthy(dependency_health.FALKORDB)
            return
        error = ConnectionError("falkordb down")
        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=error)
        raise error

    def flaky_llm_interface_probe(config: ServiceConfig) -> None:
        del config
        if llm_interface_reachable:
            dependency_health.mark_healthy(dependency_health.LLM_INTERFACE)
            return
        error = ConnectionError("llm interface down")
        dependency_health.mark_unhealthy(dependency_health.LLM_INTERFACE, error=error)
        raise error

    monkeypatch.setattr(main_module, "check_falkordb_connectivity", flaky_falkordb_probe)
    monkeypatch.setattr(main_module, "check_llm_interface_connectivity", flaky_llm_interface_probe)
    # detroit-exception: data-only gating-set widening proving generic iteration (not hardcoded)
    monkeypatch.setattr(
        main_module,
        "_GATING_DEPENDENCIES",
        (dependency_health.FALKORDB, dependency_health.LLM_INTERFACE),
    )

    with TestClient(app) as client:
        assert client.get("/ready").json()["status"] == "not_ready"

        falkordb_reachable = True

        assert client.get("/ready").json()["status"] == "not_ready"

        llm_interface_reachable = True

        assert client.get("/ready").json()["status"] == "ready"


def test_concurrent_ready_polls_while_not_ready_leave_app_state_consistent(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """AC-BI-009: concurrent `/ready` requests while `app.state.ready` is
    `False` must not corrupt `dependency_health` registry state or leave
    `app.state.ready` inconsistent. `ready()`'s retry body has no `await`
    point, so within one process no two calls ever truly interleave --
    proven here by actually racing many concurrent calls via
    `asyncio.gather` against the same running app.
    """

    def failing_falkordb_check(config: ServiceConfig) -> None:
        error = ConnectionError("boom")
        dependency_health.mark_unhealthy(dependency_health.FALKORDB, error=error)
        raise error

    monkeypatch.setattr(main_module, "check_falkordb_connectivity", failing_falkordb_check)

    ready_route = cast(
        "APIRoute", next(route for route in app.routes if getattr(route, "path", None) == "/ready")
    )

    async def poll() -> JSONResponse:
        return cast("JSONResponse", await ready_route.endpoint())

    async def poll_many() -> list[JSONResponse]:
        return await asyncio.gather(*(poll() for _ in range(20)))

    with TestClient(app):
        responses = asyncio.run(poll_many())

    assert all(response.status_code == 503 for response in responses)
    assert app.state.ready is False
    assert dependency_health.is_healthy(dependency_health.FALKORDB) is False


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

    Builds with the local-test bypass active (issue #58, Slice 5): this test
    is about transport wiring, not authentication -- `tests/mcp_interface/
    test_mcp_auth.py` covers the genuinely auth-armed mount. Without the
    bypass, `_complete_config()`'s default fake (but non-`None`)
    `auth_issuer`/`auth_audience` pair, combined with this file's own
    `_stub_resolve_auth_context` autouse fixture, would produce a real,
    armed `AuthContext`, and this unauthenticated request would now get 401
    from the MCP SDK's own gate (AC-BI-006) instead of reaching `initialize`.
    """
    app = create_app(_complete_config(is_local_test_bypass_active=True))

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

    Builds with the local-test bypass active (issue #58, Slice 5) for the
    same reason as `test_create_app_mounts_mcp_streamable_http_transport_at_fixed_path`
    above: this test is about tool/resource reachability, not
    authentication.
    """
    fake_graph = _FakeGraphHandle(result=_FakeQueryResult(header=[[0, "id"]], result_set=[["a"]]))

    def _stub_mcp_connect_from_config(_config: object) -> _FakeFalkorDB:
        return _FakeFalkorDB(fake_graph)

    monkeypatch.setattr(mcp_server, "connect_from_config", _stub_mcp_connect_from_config)

    md_file = tmp_path / "ps-domain-concepts.md"
    md_file.write_text("# PS domain concepts\n\nRegulation -> Obligation\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_domain_concepts_path", lambda: md_file)

    app = create_app(_complete_config(is_local_test_bypass_active=True))

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
    -- which drives `mcp_server.cypher()` in-process, bypassing transport
    entirely -- to the mounted Streamable HTTP transport
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
