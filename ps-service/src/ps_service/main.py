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
from importlib.metadata import version as installed_version
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.requests import Request

from ps_service.api.error_handlers import (
    _make_verbatim_handler,  # pyright: ignore[reportPrivateUsage]  # shared body-shaping helper; reused so the 413 body matches every other ApiError's shape exactly
    register_exception_handlers,
)
from ps_service.api.errors import RequestBodyTooLargeError
from ps_service.api.routes import build_api_router
from ps_service.auth.middleware import RestAuthMiddleware
from ps_service.auth.protected_resource import protected_resource_metadata
from ps_service.auth.startup import resolve_auth_context
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.config import ServiceConfig, load_config, missing_ingestion_config_fields
from ps_service.dependency_health import (
    CELLAR_ELI,
    FALKORDB,
    LLM_INTERFACE,
    all_healthy,
    is_healthy,
)
from ps_service.ingestion.adapters.cellar_eli.fetch import (
    check_connectivity as check_cellar_eli_connectivity,
)
from ps_service.ingestion.falkordb_client import (
    check_connectivity_from_config as check_falkordb_connectivity,
)
from ps_service.llm_interface import (
    check_connectivity as check_llm_interface_connectivity,
)
from ps_service.logging.facade import configure, emit_log_entry
from ps_service.mcp_interface.http_transport import MCP_HTTP_MOUNT_PATH, build_streamable_http_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

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

# Which of `_READY_DEPENDENCIES` gate `/ready`'s overall status, as opposed to only
# being named in `unhealthy_dependencies` (issue #75). A single-element tuple today
# (FalkorDB only), but `_check_dependencies_at_startup`/`_retry_gating_dependencies`
# below are written against this set, not against FalkorDB by name, so a future
# gating dependency (e.g. an identity provider) is added here and inherits both
# functions' behavior unchanged (issue #124).
_GATING_DEPENDENCIES = (FALKORDB,)


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


def _all_dependency_probes(
    config: ServiceConfig,
) -> tuple[tuple[str, Callable[[], None]], ...]:
    """The fixed (name, probe) pairs for FalkorDB, LLM Interface, and Cellar/ELI.

    The single source of truth both `_check_dependencies_at_startup` (probes
    all three, unconditionally) and `_retry_gating_dependencies` (re-probes
    only `_GATING_DEPENDENCIES`) build on, so the two never drift apart on
    which callable answers for which dependency name.
    """
    return (
        (FALKORDB, lambda: check_falkordb_connectivity(config)),
        (LLM_INTERFACE, lambda: check_llm_interface_connectivity(config)),
        (CELLAR_ELI, check_cellar_eli_connectivity),
    )


def _check_dependencies_at_startup(config: ServiceConfig) -> bool:
    """Probe FalkorDB, LLM Interface, and Cellar/ELI once at startup, logging a warning per failure.

    Returns whether every `_GATING_DEPENDENCIES` member's probe succeeded --
    the only outcome that gates `app.state.ready` (issue #75/#124,
    AC-BI-002). LLM Interface and Cellar/ELI are still probed
    unconditionally, in the same fixed order, and a failure in either is
    still logged below exactly as before -- they are simply never members of
    `_GATING_DEPENDENCIES`, so they never affect this function's return
    value. `ready()`'s live gate still reports either by name via
    `unhealthy_dependencies`, unchanged (AC-BI-003).

    Deliberately never raises (issue #22): unlike
    `configure()`'s failures above, a dependency outage must never crash the
    process, only keep it out of `/ready`'s pool. Runs every probe even
    after an earlier one fails, so a single startup gives the full picture
    of what's down rather than stopping at the first failure.

    Each probe also records its outcome in `ps_service.dependency_health`
    (`falkordb_client.check_connectivity_from_config`, `llm_interface.check_connectivity`,
    `cellar_eli.fetch.check_connectivity` all do this themselves) — that
    registry is what lets `/ready` self-heal from a later real-traffic
    success without a restart, beyond this one-time startup snapshot, and is
    also what this function's `all_healthy(_GATING_DEPENDENCIES)` return
    value reads back.
    """
    for dependency, probe in _all_dependency_probes(config):
        try:
            probe()
        except Exception as exc:  # noqa: BLE001 - a dependency outage must never crash the process (see docstring)
            emit_log_entry(
                component="entrypoint",
                action="startup",
                outcome="warning",
                extra={"dependency": dependency, "error": str(exc)},
            )
    return all_healthy(_GATING_DEPENDENCIES)


def _retry_gating_dependencies(config: ServiceConfig) -> bool:
    """Re-probe every `_GATING_DEPENDENCIES` member, returning whether all now succeed.

    Called from `ready()` while `app.state.ready` is still `False` (issue
    #124): each periodic `/ready` poll becomes a retry attempt this way,
    instead of `app.state.ready` only ever reflecting the one-time startup
    snapshot `_check_dependencies_at_startup` took. Fixes the exact race
    `spikes/deploy-ps-azure/README.md` documented, where FalkorDB became
    reachable seconds after ps-service's own startup probe had already
    failed and latched `not_ready` for the rest of the process's life.

    Deliberately never raises, mirroring `_check_dependencies_at_startup`:
    a still-down dependency must keep `/ready` at `503`, not fail the
    request that was only trying to check.
    """
    gating_probes = dict(_all_dependency_probes(config))
    for dependency in _GATING_DEPENDENCIES:
        try:
            gating_probes[dependency]()
        except Exception as exc:  # noqa: BLE001 - see docstring: a retry failure must not fail the request
            emit_log_entry(
                component="entrypoint",
                action="ready_retry",
                outcome="warning",
                extra={"dependency": dependency, "error": str(exc)},
            )
    return all_healthy(_GATING_DEPENDENCIES)


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
    leakage), and the `ps_service.api` REST router (`GET /catalog`, and, in
    later increments, `POST /ingestions`) is mounted via `app.include_router`.
    `/health` and `/ready` stay on `app.add_api_route` — they predate the
    router and carry no request models.

    Since issue #39, `build_streamable_http_app(host=config.host, verifier=...,
    auth_context=...)` builds MCP Interface's Streamable HTTP ASGI sub-app
    (wrapping the same `mcp_server.server` singleton `mcp_interface`
    defines), mounted unconditionally at `MCP_HTTP_MOUNT_PATH` (`/mcp`)
    alongside the REST router — the same process/port, never a second
    service (AC-BI-002). Since issue #58, `verifier`/`auth_context` are the
    exact same instances passed to `RestAuthMiddleware` below, so `/mcp`
    requests go through the MCP SDK's own `token_verifier=` gate
    (AC-BI-006), sharing the one process-wide verifier/JWKS cache.
    Because Starlette does not propagate a mounted sub-app's own `lifespan`
    (verified directly against this repo's installed `starlette` version),
    `lifespan` below explicitly enters
    `mcp_asgi_app.router.lifespan_context(mcp_asgi_app)` around its existing
    tail, so the SDK's Streamable HTTP session manager only starts after
    `_refuse_non_loopback_bypass_bind`, `configure(...)`, and the startup
    warning entries have already run — preserving AC-BI-004/005's fail-fast
    ordering.

    Since issue #58, `resolve_auth_context(config)` runs unconditionally
    here (AC-BI-001/AC-BI-002): if the local-test bypass (issue #67) is
    inactive and `PS_AUTH_ISSUER`/`PS_AUTH_AUDIENCE` are not both set, this
    raises `AuthConfigurationError` and `create_app` never returns an app --
    the exception propagates straight out of `main()`'s call site. The
    resolved (possibly `None`, for an active bypass) result is stashed on
    `app.state.auth_context`. `RestAuthMiddleware` (AC-BI-003/AC-BI-004) is
    then added, wired to a `PsTokenVerifier` built from that same
    `AuthContext` (or `None`, when the bypass is active -- every request is
    let through unauthenticated, matching issue #67's existing contract):
    every path other than `/health`, `/ready`, `/.well-known/*`, and `/mcp*`
    (delegated to the MCP SDK's own `token_verifier=` gate, Slice 5) now
    requires a verified bearer token.

    Since Slice 8 (AC-BI-010), `GET /.well-known/oauth-protected-resource`
    is registered here too, alongside `/health`/`/ready` -- the exact URL
    `RestAuthMiddleware`'s `WWW-Authenticate` header already names. It is
    the *only* route publishing RFC 9728 metadata: `build_streamable_http_app`
    leaves `AuthSettings.resource_server_url=None` (Slice 5), so the MCP SDK
    never auto-registers a competing one under `/mcp`.
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
        (issue #22) confirms every `_GATING_DEPENDENCIES` member is reachable
        AND every `INGESTION_REQUIRED_CONFIG_FIELDS` value resolved (issue
        #16 follow-up) — LLM Interface and Cellar/ELI are still probed
        unconditionally at startup and still logged on failure, but neither
        one's outcome affects this flag (issue #75, AC-BI-002): a transient
        LLM/Cellar-ELI outage at boot must not wedge readiness for the rest
        of the process's life. Unlike `configure()` above, neither a
        dependency failure nor incomplete config propagates here: each only
        keeps this instance out of `/ready`'s pool, preserving
        liveness/readiness's whole reason for existing (a dependency outage,
        or an incomplete deploy, must never crash-loop an otherwise-healthy
        process).

        Missing config is checked once here, not folded into
        `dependency_health`'s live-updating registry: `config` is a frozen
        `ServiceConfig` resolved once by `load_config()` before `create_app`
        is even called, so unlike dependency reachability it cannot change,
        recover, or need re-probing for the life of this process — a
        one-time startup check is the whole story. Its result is stashed on
        `app.state.config_complete` rather than only folded into this one
        `app.state.ready` assignment, because `ready()` (issue #124) reads
        it again on every later retry attempt: `app.state.ready` itself can
        now flip `True` after this function returns (once a gating
        dependency recovers), and `app.state.config_complete` being `False`
        must keep blocking that forever, exactly as it blocks it here.
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
        app.state.config_complete = not missing_config
        async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
            app.state.ready = _check_dependencies_at_startup(config) and app.state.config_complete
            yield
            app.state.ready = False

    app = FastAPI(lifespan=lifespan)
    app.state.ready = False
    app.state.config_complete = False
    app.state.config = config
    # AC-BI-001/AC-BI-002: resolved synchronously here, not inside the async `lifespan`
    # closure, so that a bare (never-entered) `TestClient`/ASGI middleware added in a
    # later slice still has a concrete auth decision the moment the app exists. Stashed
    # on `app.state` for a later slice's middleware/route wiring to consume.
    auth_context = resolve_auth_context(config)
    app.state.auth_context = auth_context
    # One `PsTokenVerifier` instance per `create_app()` call, never module-level (the
    # import-time-hazard rule PLAN.md §0.1 documents) -- this exact instance is what
    # both `RestAuthMiddleware` below and, from Slice 5 onward, the MCP
    # `token_verifier=` wiring share, so there is one discovery fetch, one
    # `PyJWKClient`, one JWKS cache per process, never two parallel verifiers.
    verifier = PsTokenVerifier(auth_context) if auth_context is not None else None
    # `RestAuthMiddleware` is added *before* `_MaxBodySizeMiddleware` so the latter
    # stays the outermost, first-to-run layer (CHANGES.md item 6): `add_middleware`
    # makes the most-recently-added call the outermost, so an oversized request gets
    # a cheap 413 on its `Content-Length` header before any JWT/RSA verification work
    # happens -- there is no data dependency the other way (the size check never reads
    # `Authorization`), so this ordering costs nothing and avoids wasted verify work.
    app.add_middleware(RestAuthMiddleware, verifier=verifier, auth_context=auth_context)
    app.add_middleware(_MaxBodySizeMiddleware, max_bytes=config.max_request_body_bytes)
    register_exception_handlers(app)
    app.include_router(build_api_router())

    mcp_asgi_app = build_streamable_http_app(
        host=config.host, verifier=verifier, auth_context=auth_context
    )
    app.mount(MCP_HTTP_MOUNT_PATH, mcp_asgi_app)

    async def health() -> dict[str, str]:
        """Report liveness: "alive" as soon as the ASGI server accepts connections.

        Must never depend on `lifespan` startup progress or check external
        dependencies — a dependency outage must never fail liveness. `version`
        reads this process's own installed distribution metadata
        (`importlib.metadata.version`) -- no I/O, no network, no dependency
        call, so it introduces no new liveness risk (AC-BI-001/AC-BI-002).
        """
        return {"status": "alive", "version": installed_version("ps-service")}

    async def ready() -> JSONResponse:
        """Report "ready" only once startup succeeded AND every gating dependency is healthy now.

        Two independent gates (issue #22): `app.state.ready` AND the live
        `dependency_health` registry's `_GATING_DEPENDENCIES` entries
        (updated by real traffic as it happens, read via `all_healthy`) both
        have to hold. The live gate is what lets `/ready` flip back to
        `not_ready` if a gating dependency fails mid-run, and self-heal on
        its next success, without waiting for a restart.

        Unlike the live gate, `app.state.ready` used to be a pure one-time
        snapshot from `lifespan`'s startup probe — which is exactly what let
        it latch `False` forever if a gating dependency was still down at
        boot, even once it recovered seconds later (issue #124, the race
        `spikes/deploy-ps-azure/README.md` documented). While it is still
        `False`, this handler now re-runs `_retry_gating_dependencies` on
        every call, so each periodic `/ready` poll is itself a retry
        attempt — no separate background task needed, since Kubernetes'
        own `readinessProbe` cadence drives it. `app.state.config_complete`
        (issue #16 follow-up, stashed once in `lifespan`) gates the retry
        itself: it cannot change mid-run, so once it is `False` no amount of
        dependency recovery may ever flip `app.state.ready` `True`. Once
        `app.state.ready` does flip `True`, it is never reset back to
        `False` by this handler again (only `lifespan`'s shutdown does) —
        from that point on, live degradation is caught solely by the
        `all_healthy(_GATING_DEPENDENCIES)` half below, exactly as before
        issue #124. LLM Interface and Cellar/ELI are deliberately excluded
        from `_GATING_DEPENDENCIES` (issue #75): they are still probed at
        startup and still tracked live in `dependency_health`, but neither
        their startup nor live health ever flips `/ready`'s status.

        `unhealthy_dependencies` (issue #68) names every currently-unhealthy
        member of `_READY_DEPENDENCIES` (still all three dependencies, issue
        #75 does not change this list) by its `dependency_health` constant
        string, read directly off the live registry via `is_healthy` — never
        the raw error text `mark_unhealthy` stores, which stays private to
        `dependency_health`. Always present, empty when every dependency is
        healthy, so callers get one predictable shape rather than two
        distinguished by key presence. A dependency may appear here even
        while `status` stays `"ready"`, when it is LLM Interface or
        Cellar/ELI.

        Returns an actual non-2xx status (`503`) when `status` is
        `"not_ready"` (issue #75) — the response body alone previously left
        an always-200 `/ready` unable to pull the pod from Kubernetes
        Service rotation on a real outage, since the chart's `readinessProbe`
        is a plain body-blind `httpGet`.
        """
        if not app.state.ready and app.state.config_complete:
            app.state.ready = _retry_gating_dependencies(config)
        unhealthy_dependencies = [
            dependency for dependency in _READY_DEPENDENCIES if not is_healthy(dependency)
        ]
        is_ready = app.state.ready and all_healthy(_GATING_DEPENDENCIES)
        return JSONResponse(
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "ready" if is_ready else "not_ready",
                "unhealthy_dependencies": unhealthy_dependencies,
            },
        )

    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/ready", ready, methods=["GET"])
    app.add_api_route(
        "/.well-known/oauth-protected-resource",
        protected_resource_metadata,
        methods=["GET"],
        response_model_exclude_none=True,
    )

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
