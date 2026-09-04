"""PS Service process harness and composition root: the FastAPI app.

Owns liveness/readiness (`/health`, `/ready`) and, since issue #51, mounts the
`ps_service.api` REST router into `create_app` (the one deliberate
`ps_service.api` import). Since issue #39, it also mounts MCP Interface's
Streamable HTTP transport (`ps_service.mcp_interface.http_transport`) into
the same app -- a second deliberate exception to the same pattern. It still
has no module-load import of, and no readiness relationship with, Domain
Mapper, Company Merge, Query Engine, or Regulatory Change Monitor.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, status
from starlette.datastructures import Headers
from starlette.requests import Request

from ps_service.api.error_handlers import (
    _make_verbatim_handler,  # pyright: ignore[reportPrivateUsage]  # shared body-shaping helper; reused so the 413 body matches every other ApiError's shape exactly
    register_exception_handlers,
)
from ps_service.api.errors import RequestBodyTooLargeError
from ps_service.api.routes import build_api_router
from ps_service.config import ServiceConfig, load_config, missing_ingestion_config_fields
from ps_service.dependency_health import (
    CELLAR_ELI,
    FALKORDB,
    LLM_INTERFACE,
    is_healthy,
)
from ps_service.ingestion.adapters.cellar_eli.fetch import (
    check_connectivity as check_cellar_eli_connectivity,
)
from ps_service.ingestion.falkordb_client import (
    check_connectivity as check_falkordb_connectivity,
)
from ps_service.ingestion.falkordb_client import connect_from_config
from ps_service.llm_interface import (
    check_connectivity as check_llm_interface_connectivity,
)
from ps_service.logging.facade import configure, emit_log_entry
from ps_service.mcp_interface.http_transport import MCP_HTTP_MOUNT_PATH, build_streamable_http_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.types import ASGIApp, Receive, Scope, Send

# Matches `_DEFAULT_LOG_FILENAME` in `ps_service/logging/facade.py` and the sink filename documented
# in `docs/architecture/ps-service-container-architecture.md`. Kept as a local literal (not
# imported) so this fix stays within main.py, without touching the already-shipped, already-reviewed
# Logging component's private API surface.
_LOG_FILENAME = "ps-service.jsonl"

_REQUEST_BODY_TOO_LARGE_HANDLER = _make_verbatim_handler(
    "request_body_too_large", status.HTTP_413_CONTENT_TOO_LARGE
)
"""The exact same body-shaping handler `register_exception_handlers` would wire up for
`RequestBodyTooLargeError` (`error_handlers._API_ERROR_SPECS`) -- reused directly by
`_MaxBodySizeMiddleware` below, since a pure ASGI middleware added via `app.add_middleware`
sits OUTSIDE Starlette's `ExceptionMiddleware` (confirmed empirically against this repo's
installed `starlette`/`fastapi`: an exception raised from a middleware never reaches a
type-specific `add_exception_handler` registration -- it is caught by the outer
`ServerErrorMiddleware` and collapses to this app's generic catch-all `Exception` handler
instead, a 500 with no `request_body_too_large` code). CHANGES.md OQ7's own sketch has the
middleware simply `raise RequestBodyTooLargeError(...)`; that raise alone would NOT
actually reach the 413 mapping in this codebase's app (which registers a catch-all
`Exception` handler) -- calling this handler function directly and sending its response
ourselves is the fix that makes OQ7's stated intent (413, not 500) real."""


class _MaxBodySizeMiddleware:
    """Pure ASGI middleware rejecting an oversized request body (CHANGES.md OQ7).

    Deliberately not `BaseHTTPMiddleware`, which buffers the whole body before
    a route ever sees it -- this inspects the `Content-Length` header alone,
    before Starlette reads any body bytes at all. Checks `Content-Length`
    only, not a streamed byte count: correct for `ps-cli`'s `httpx`-based
    `POST /restorations` call (a fixed-length JSON body, never
    chunked-transfer-encoded); a client that omits `Content-Length` and
    streams indefinitely is a residual, explicitly accepted gap (OQ7).
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        """Wrap `app`, rejecting any HTTP request whose `Content-Length` exceeds `max_bytes`."""
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject an oversized request with a 413 before `self._app` ever runs."""
        if scope["type"] == "http":
            content_length = Headers(scope=scope).get("content-length")
            if content_length is not None and int(content_length) > self._max_bytes:
                exc = RequestBodyTooLargeError(
                    f"request body exceeds the {self._max_bytes}-byte limit"
                )
                response = await _REQUEST_BODY_TOO_LARGE_HANDLER(Request(scope, receive), exc)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_READY_DEPENDENCIES = (FALKORDB, LLM_INTERFACE, CELLAR_ELI)


class LocalTestBypassBindRefusedError(Exception):
    """The local-test bypass is active but `config.host` is not loopback (AC-BI-002)."""


def _is_loopback(host: str) -> bool:
    """Return whether `host` is one of the recognized loopback spellings.

    Exact string match only against the three spellings a developer/operator
    would plausibly type for "this machine only" (IPv4 loopback literal, the
    conventional hostname, IPv6 loopback literal) — deliberately not full
    `127.0.0.0/8` range matching, which would need `ipaddress` parsing for
    marginal benefit no AC asks for.
    """
    return host in _LOOPBACK_HOSTS


def _refuse_non_loopback_bypass_bind(config: ServiceConfig) -> None:
    """Raise `LocalTestBypassBindRefusedError` if the bypass is active on a non-loopback host.

    AC-BI-002: the local-test bypass is unauthenticated, so it must never be
    reachable from beyond this machine. AC-BI-003: the message states both
    facts (bypass active, host not loopback) so the operator can identify the
    fix immediately. AC-BI-004: when the bypass is inactive (the default),
    this check short-circuits and does nothing, leaving the existing
    warning-only non-loopback handling in `lifespan()` completely unchanged.

    Called at two defense-in-depth points (L1's security-critical-sink
    carve-out): `main()`, before `create_app`/`uvicorn.run` are ever reached,
    and `lifespan()`'s first statement, so any caller that builds
    `create_app(config)` directly (as every unit test in `test_main.py` does)
    is covered too.
    """
    if config.is_local_test_bypass_active and not _is_loopback(config.host):
        message = (
            f"local-test bypass is active AND configured host {config.host!r} "
            "is not loopback -- refusing to start (would expose the unauthenticated "
            "bypass beyond this machine)"
        )
        raise LocalTestBypassBindRefusedError(message)


def _check_dependencies_at_startup(config: ServiceConfig) -> bool:
    """Probe FalkorDB, LLM Interface, and Cellar/ELI once at startup, logging a warning per failure.

    Deliberately never raises (issue #22): unlike
    `configure()`'s failures above, a dependency outage must never crash the
    process, only keep it out of `/ready`'s pool. Runs every probe even
    after an earlier one fails, so a single startup gives the full picture
    of what's down rather than stopping at the first failure.

    Each probe also records its outcome in `ps_service.dependency_health`
    (`falkordb_client.check_connectivity`, `llm_interface.check_connectivity`,
    `cellar_eli.fetch.check_connectivity` all do this themselves) — that
    registry is what lets `/ready` self-heal from a later real-traffic
    success without a restart, beyond this one-time startup snapshot.
    """
    all_succeeded = True
    for dependency, probe in (
        (
            FALKORDB,
            lambda: check_falkordb_connectivity(
                connect_from_config(config), config.falkordb_host, config.falkordb_port
            ),
        ),
        (LLM_INTERFACE, lambda: check_llm_interface_connectivity(config)),
        (CELLAR_ELI, check_cellar_eli_connectivity),
    ):
        try:
            probe()
        except Exception as exc:  # noqa: BLE001 - a dependency outage must never crash the process (see docstring)
            all_succeeded = False
            emit_log_entry(
                component="entrypoint",
                action="startup",
                outcome="warning",
                extra={"dependency": dependency, "error": str(exc)},
            )
    return all_succeeded


def create_app(config: ServiceConfig) -> FastAPI:
    """Build a FastAPI app instance wired to the given `ServiceConfig`.

    A factory rather than a module-level singleton so that two independently
    configured apps can coexist without sharing mutable state (e.g. two
    `TestClient`s in the same test file): `lifespan` becomes a closure
    capturing `config` by reference, and the readiness flag lives on
    `app.state` (Starlette's per-instance `State` object) rather than a
    module global.

    `config.logging_dir` (joined with the fixed log filename) is threaded
    explicitly into `configure(log_path=...)` below, so two `create_app()`
    calls with different `logging_dir`s never leak into each other's log
    sink. If `config.host` is not one of the recognized loopback spellings
    (see `_is_loopback`), an additional warning-level startup log entry is
    emitted, noting this unauthenticated harness is binding beyond localhost.

    The resolved `config` is stashed on `app.state.config` (read back by the
    REST layer's `get_service_config` dependency),
    `register_exception_handlers(app)` installs the `ps_service.api` structured
    4xx/5xx error handlers (AC-BI-009: no stack-trace / path / infra-detail
    leakage), and the `ps_service.api` REST router (`GET /regulations`, and, in
    later increments, `POST /ingestions`) is mounted via `app.include_router`.
    `/health` and `/ready` stay on `app.add_api_route` — they predate the
    router and carry no request models.

    Since issue #39, `build_streamable_http_app(host=config.host)` builds MCP
    Interface's Streamable HTTP ASGI sub-app (wrapping the same
    `mcp_server.server` singleton the stdio entrypoint uses), mounted
    unconditionally at `MCP_HTTP_MOUNT_PATH` (`/mcp`) alongside the REST
    router — the same process/port, never a second service (AC-BI-002).
    Because Starlette does not propagate a mounted sub-app's own `lifespan`
    (verified directly against this repo's installed `starlette` version),
    `lifespan` below explicitly enters
    `mcp_asgi_app.router.lifespan_context(mcp_asgi_app)` around its existing
    tail, so the SDK's Streamable HTTP session manager only starts after
    `_refuse_non_loopback_bypass_bind`, `configure(...)`, and the startup
    warning entries have already run — preserving AC-BI-004/005's fail-fast
    ordering.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Configure Logging, emit startup entries, probe dependencies once, then flip readiness.

        `configure(log_path=...)` is called before any `emit_log_entry` call,
        per the Logging facade's contract that a process-wide default emitter
        must be installed before any `emit_log_entry` call. `config.logging_dir`
        is a *directory* (matching `PS_LOGGING_DIR`'s existing env-var
        semantics), while `configure()`'s `log_path` parameter is a literal
        *file* path with no directory-to-file join of its own when given a
        non-`None` value (that join only happens inside
        `resolve_default_log_path()`, which only runs when `log_path=None`) —
        so `config.logging_dir` is joined with the fixed filename
        `_LOG_FILENAME` here before being passed through. Threading this
        explicitly (rather than bare `configure()`) is what makes readiness
        AND logging configuration both flow from this instance's `config`,
        not from `Logging`'s own environment-derived fallback. If
        `config.host` resolves non-loopback, an additional
        `outcome="warning"` entry is emitted before the unconditional
        `outcome="success"` entry (AC-BI-011); if the local-test bypass is
        active, a further `outcome="warning"` entry is emitted stating both
        facts an operator needs (`local_test_bypass_active=True`,
        `bind_scope="loopback-only"`) — every process start, not only the
        first, since nothing here gates on prior-warning state (AC-BI-007) —
        before the unconditional `outcome="success"` entry. Both warnings are
        additive, never replacing the success entry. Any exception raised by
        `configure()` (e.g. `LoggingConfigurationError`) is deliberately left
        to propagate — fail-fast (L1) — rather than swallowed. Uvicorn's own
        startup-failure path reports it to stderr.

        `app.state.ready` only flips `True` once `_check_dependencies_at_startup`
        (issue #22) confirms FalkorDB, LLM Interface, and Cellar/ELI are all
        reachable AND every `INGESTION_REQUIRED_CONFIG_FIELDS` value resolved
        (issue #16 follow-up) — unlike `configure()` above, neither failure
        here propagates: each only keeps this instance out of `/ready`'s
        pool, preserving liveness/readiness's whole reason for existing (a
        dependency outage, or an incomplete deploy, must never crash-loop an
        otherwise-healthy process).

        Missing config is checked once here, not folded into
        `dependency_health`'s live-updating registry: `config` is a frozen
        `ServiceConfig` resolved once by `load_config()` before `create_app`
        is even called, so unlike dependency reachability it cannot change,
        recover, or need re-probing for the life of this process — a
        one-time startup check is the whole story.
        """
        _refuse_non_loopback_bypass_bind(config)
        log_path = (config.logging_dir / _LOG_FILENAME) if config.logging_dir is not None else None
        configure(log_path=log_path)
        if not _is_loopback(config.host):
            emit_log_entry(
                component="entrypoint",
                action="startup",
                outcome="warning",
                extra={"host": config.host},
            )
        if config.is_local_test_bypass_active:
            emit_log_entry(
                component="entrypoint",
                action="startup",
                outcome="warning",
                extra={"local_test_bypass_active": True, "bind_scope": "loopback-only"},
            )
        emit_log_entry(component="entrypoint", action="startup", outcome="success")
        missing_config = missing_ingestion_config_fields(config)
        if missing_config:
            emit_log_entry(
                component="entrypoint",
                action="startup",
                outcome="warning",
                extra={"missing_config": missing_config},
            )
        async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
            app.state.ready = _check_dependencies_at_startup(config) and not missing_config
            yield
            app.state.ready = False

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(_MaxBodySizeMiddleware, max_bytes=config.max_request_body_bytes)
    app.state.ready = False
    app.state.config = config
    register_exception_handlers(app)
    app.include_router(build_api_router())

    mcp_asgi_app = build_streamable_http_app(host=config.host)
    app.mount(MCP_HTTP_MOUNT_PATH, mcp_asgi_app)

    async def health() -> dict[str, str]:
        """Report liveness: "alive" as soon as the ASGI server accepts connections.

        Must never depend on `lifespan` startup progress or check external
        dependencies — a dependency outage must never fail liveness.
        """
        return {"status": "alive"}

    async def ready() -> dict[str, str | list[str]]:
        """Report "ready" only once startup succeeded AND every dependency is currently healthy.

        Two independent gates (issue #22): `app.state.ready` (the one-time
        startup probe from `lifespan` — which itself folds in both the three
        dependency probes AND ingestion config completeness, issue #16
        follow-up) AND the live `dependency_health` registry (updated by real
        FalkorDB/LLM Interface/Cellar-ELI traffic as it happens) both have to
        hold. The live gate is what lets `/ready` flip back to `not_ready` if
        a dependency fails mid-run, and self-heal on its next success,
        without waiting for a restart — config completeness has no
        equivalent live gate because it cannot change mid-run (see
        `lifespan`'s docstring), so `app.state.ready` alone is the whole
        story for that half.

        `unhealthy_dependencies` (issue #68) names every currently-unhealthy
        member of `_READY_DEPENDENCIES` by its `dependency_health` constant
        string, read directly off the live registry via `is_healthy` — never
        the raw error text `mark_unhealthy` stores, which stays private to
        `dependency_health`. Always present, empty when every dependency is
        healthy, so callers get one predictable shape rather than two
        distinguished by key presence.
        """
        unhealthy_dependencies = [
            dependency for dependency in _READY_DEPENDENCIES if not is_healthy(dependency)
        ]
        is_ready = app.state.ready and not unhealthy_dependencies
        return {
            "status": "ready" if is_ready else "not_ready",
            "unhealthy_dependencies": unhealthy_dependencies,
        }

    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/ready", ready, methods=["GET"])

    return app


def main() -> None:
    """Resolve configuration once, build the app, and run it under uvicorn.

    This is the process's composition root: `load_config()` is called
    exactly once, resolving the full `PS_SERVICE_*`/`PS_LOGGING_DIR` config
    surface, and the resulting `ServiceConfig` is injected explicitly into
    both `create_app()` and `uvicorn.run()` — no component downstream reads
    `os.environ` independently. SIGTERM/SIGINT handling is delegated
    entirely to uvicorn's built-in signal handling — no custom `asyncio`
    signal handling here, per the L2 coding standard's Entrypoint / Process
    Lifecycle Patterns section.
    """
    config = load_config()
    _refuse_non_loopback_bypass_bind(config)
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        timeout_graceful_shutdown=config.graceful_shutdown_seconds,
    )
