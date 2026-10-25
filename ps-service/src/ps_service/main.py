"""PS Service process harness and composition root: the FastAPI app.

Owns the in-cluster liveness and readiness probes (`/livez`, `/readyz`), publishes the recorded
readiness the REST `/status` handler reports, and builds the one ASGI app (`create_app`)
that serves the `ps_service.api` REST router, the companion-browser signing-ceremony router
(`ps_service.passkey_signing.router`) and the MCP Interface Streamable HTTP transport
(`ps_service.mcp_interface.http_transport`) on one process and one port. Those are the
deliberate component imports of this module. It has no module-load import of, and no readiness
relationship with, Domain Mapper, Company Merge, Query Engine, or Regulatory Change Monitor.

`main()` is the process entrypoint: it resolves configuration once, builds the app and runs it
under uvicorn.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

import uvicorn
from fastapi import FastAPI, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.requests import Request

from ps_service.api.error_handlers import (
    _make_verbatim_handler,  # pyright: ignore[reportPrivateUsage]  # shared body-shaping helper; reused so the 413 body matches every other ApiError's shape exactly
    register_exception_handlers,
)
from ps_service.api.errors import RequestBodyTooLargeError
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.api.routes import build_api_router
from ps_service.audit import AUDIT_LOG_SCHEMAS, AUDIT_LOG_TABLES
from ps_service.auth.middleware import RestAuthMiddleware
from ps_service.auth.protected_resource import (
    protected_resource_metadata,
    protected_resource_metadata_for_path,
)
from ps_service.auth.startup import require_public_url_configured, resolve_auth_context
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.authz.startup import require_bootstrap_owner_configured
from ps_service.config import ServiceConfig, load_config, missing_ingestion_config_fields
from ps_service.dependency_health import (
    CELLAR_ELI,
    FALKORDB,
    GRAPH_REPLAY,
    LLM_INTERFACE,
    PASSKEY_SIGNING_POSTGRES,
    STATE_POSTGRES,
    all_healthy,
    is_healthy,
    mark_healthy,
    mark_unhealthy,
)
from ps_service.gating_reprobe import GatingReprobe
from ps_service.graph_gateway import GRAPH_LOG_TABLES
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.pending_entries import count_pending_entries
from ps_service.ingestion.adapters.cellar_eli.fetch import (
    check_connectivity as check_cellar_eli_connectivity,
)
from ps_service.ingestion.falkordb_client import (
    check_connectivity_from_config as check_falkordb_connectivity,
)
from ps_service.invitations.startup import require_authentik_credential_configured
from ps_service.llm_interface import (
    check_connectivity as check_llm_interface_connectivity,
)
from ps_service.logging.facade import configure, emit_log_entry
from ps_service.mcp_interface.http_transport import MCP_HTTP_MOUNT_PATH, build_streamable_http_app
from ps_service.mcp_interface.mcp_server import server
from ps_service.metrics import build_metrics, build_metrics_router
from ps_service.metrics.catalog import REQUEST_DURATION
from ps_service.metrics.http_middleware import MetricsMiddleware
from ps_service.metrics.sources import (
    PENDING_CONNECT_TIMEOUT_S,
    PENDING_SOURCE_DEADLINE_S,
    PENDING_STATEMENT_TIMEOUT_MS,
    PENDING_TTL_S,
    TtlCachedSource,
)
from ps_service.passkey_signing.migration_runner import apply_pending_migrations
from ps_service.passkey_signing.router import build_passkey_signing_router
from ps_service.passkey_signing.store import (
    check_connectivity_from_config as check_passkey_signing_postgres_connectivity,
)
from ps_service.passkey_signing.store import (
    connect_for_migrations as connect_passkey_signing_postgres_for_migrations,
)
from ps_service.persistence import PrivilegedMigrationMissingError
from ps_service.persistence import (
    apply_pending_migrations as apply_state_migrations,
)
from ps_service.persistence import (
    check_connectivity_from_config as check_state_postgres_connectivity,
)
from ps_service.persistence import (
    connect_for_migrations as connect_state_postgres_for_migrations,
)
from ps_service.persistence.privileged_migration_runner import (
    verify_privileged_migrations_applied,
)
from ps_service.state_migrations import (
    AUDIT_INTEGRITY_MIGRATION_SOURCE,
    GRAPH_GATEWAY_MIGRATION_SOURCE,
    ORDINARY_STATE_MIGRATION_SOURCES,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    import psycopg
    from psycopg.rows import TupleRow
    from starlette.types import ASGIApp, Receive, Scope, Send

    from ps_service.auth.models import AuthContext
    from ps_service.graph_gateway.models import StartupReplayReport
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.metrics import MetricsHandle

# Matches `_DEFAULT_LOG_FILENAME` in `ps_service/logging/facade.py` and the sink filename documented
# in `docs/architecture/ps-service-container-architecture.md`. Kept as a local literal (not
# imported) so this module does not depend on the Logging component's private API surface.
_LOG_FILENAME = "ps-service.jsonl"

_REQUEST_BODY_TOO_LARGE_HANDLER = _make_verbatim_handler(
    "request_body_too_large", status.HTTP_413_CONTENT_TOO_LARGE
)
"""The body-shaping handler `register_exception_handlers` wires up for `RequestBodyTooLargeError`.

`_MaxBodySizeMiddleware` calls it directly and sends its response itself: a pure ASGI middleware
added via `app.add_middleware` sits OUTSIDE Starlette's `ExceptionMiddleware`, so an exception
raised from it never reaches a type-specific `add_exception_handler` registration. It would be
caught by the outer `ServerErrorMiddleware` and collapse to this app's generic catch-all
`Exception` handler, a 500 without the `request_body_too_large` code, instead of the intended 413.
"""


class _MaxBodySizeMiddleware:
    """Pure ASGI middleware that rejects a request whose declared body exceeds a byte limit.

    Deliberately not `BaseHTTPMiddleware`, which buffers the whole body before a route sees it:
    this inspects the `Content-Length` header alone, before Starlette reads any body bytes.
    It checks `Content-Length` only, not a streamed byte count: that is correct for a client
    sending a fixed-length JSON body, and a client that omits `Content-Length` and streams
    indefinitely is a residual, accepted gap.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        """Wrap `app`, rejecting any HTTP request whose `Content-Length` exceeds `max_bytes`.

        Args:
            app: The ASGI application to wrap.
            max_bytes: The largest accepted `Content-Length`, in bytes; a larger value is rejected.
        """
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 413 for an oversized HTTP request; otherwise pass the call to the wrapped app.

        Args:
            scope: The ASGI connection scope; only `http` scopes are checked, others pass through.
            receive: The ASGI receive callable, passed on unchanged.
            send: The ASGI send callable, used to write the 413 response on rejection.

        Raises:
            ValueError: If the `Content-Length` header is present but not an integer.

        Side Effects:
            Writes the 413 response (body shaped like every other API error) to `send` and does
            not call the wrapped app when the declared length exceeds the limit.
        """
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


_REPLAY_GATE_EXEMPT_PATHS = frozenset({"/livez", "/readyz", "/status", "/metrics"})
_REPLAY_GATE_EXEMPT_PREFIX = "/.well-known/"
_REPLAY_GATE_RETRY_AFTER_SECONDS = "5"


class _ReplayReadGateMiddleware:
    """Pure ASGI middleware that serves nothing graph-related while the startup replay runs.

    While `graph_replay` is unhealthy a graph may be empty or half rebuilt, so any request that
    could read it gets `503` with the status-only `/readyz` body. Exempt: `/livez`, `/readyz`,
    `/status`, `/metrics` and the `/.well-known/` metadata, the only paths that are
    unauthenticated (or, for `/status`, verify an optional token in their own handler) and
    graph-free. It is the outermost layer, so it runs before authentication: a caller who is not
    authenticated learns nothing beyond "not ready". A dependency that was never recorded counts
    healthy, so a service without a replay is never gated. Only HTTP scopes are looked at.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`.

        Args:
            app: The ASGI application to wrap.
        """
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 503 for a gated path while `graph_replay` is unhealthy; else call `self._app`.

        Args:
            scope: The ASGI connection scope; only `http` scopes on a gated path are answered 503.
            receive: The ASGI receive callable, passed on unchanged.
            send: The ASGI send callable, used to write the 503 response when the request is gated.

        Side Effects:
            Writes a 503 response with a `Retry-After` header to `send` and does not call the
            wrapped app when the request is gated.
        """
        if scope["type"] == "http" and not is_healthy(GRAPH_REPLAY) and _is_gated_path(scope):
            response = JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                headers={"Retry-After": _REPLAY_GATE_RETRY_AFTER_SECONDS},
                content={"status": "not_ready"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _is_gated_path(scope: Scope) -> bool:
    path = str(scope["path"])
    return path not in _REPLAY_GATE_EXEMPT_PATHS and not path.startswith(_REPLAY_GATE_EXEMPT_PREFIX)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_READY_DEPENDENCIES = (
    FALKORDB,
    LLM_INTERFACE,
    CELLAR_ELI,
    PASSKEY_SIGNING_POSTGRES,
    STATE_POSTGRES,
    GRAPH_REPLAY,
)

# Which of `_READY_DEPENDENCIES` gate `/readyz`'s overall status, as opposed to only being named in
# `unhealthy_dependencies`. FalkorDB and PS state Postgres gate; `_check_dependencies_at_startup`
# and the `/readyz` re-probe (`GatingReprobe`) are written against this set, not against any
# dependency by name, so a future gating dependency (e.g. an identity provider) is added here and
# inherits both behaviors unchanged.
# PS state Postgres gates because it backs every privileged read and write (access roles, the
# audit trail, runtime config incl. the catalog-source override), so a pod with no usable state
# store must not enter Service rotation -- and a first `helm install` must not report ready
# before the store is provisioned. Each action's own fail-closed check of the state store stays.
# Passkey Signing Postgres is deliberately NOT added here: an outage of this optional,
# pilot-scope instance must never block `/readyz` for the rest of the system -- only the one
# gated merge path degrades.
# Startup replay of the graph mutation log gates as well: until it ends, graphs may be
# empty or half rebuilt, so the pod must not take traffic. Its state is owned by the replay task
# (see `_start_graph_replay_at_startup`), not by a probe.
_GATING_DEPENDENCIES = (FALKORDB, STATE_POSTGRES, GRAPH_REPLAY)


def _unhealthy_dependency_counts() -> tuple[int, int]:
    """Count unhealthy `_READY_DEPENDENCIES` as `(gating, non_gating)`, for the metrics gauge.

    A pure read of the dependency-health registry: it probes nothing and marks nothing, so a
    scrape cannot change what `/readyz` reports. Only counts leave this function, never names.

    Returns:
        The number of unhealthy gating dependencies and of unhealthy non-gating ones.
    """
    unhealthy = [dependency for dependency in _READY_DEPENDENCIES if not is_healthy(dependency)]
    gating = sum(1 for dependency in unhealthy if dependency in _GATING_DEPENDENCIES)
    return gating, len(unhealthy) - gating


class LocalTestBypassBindRefusedError(Exception):
    """Raised at startup when the local-test bypass is active but `config.host` is not loopback.

    The bypass lets every request through unauthenticated, so it must never be reachable from
    beyond this machine; the process refuses to start instead.
    """


def _is_loopback(host: str) -> bool:
    """Return whether `host` is one of the recognized loopback spellings.

    Exact string match only against the three spellings an operator would plausibly type for
    "this machine only" (IPv4 loopback literal, the conventional hostname, IPv6 loopback
    literal); deliberately not full `127.0.0.0/8` range matching.

    Args:
        host: The configured bind host.

    Returns:
        `True` if `host` is `127.0.0.1`, `localhost` or `::1`; `False` otherwise.
    """
    return host in _LOOPBACK_HOSTS


def _refuse_non_loopback_bypass_bind(config: ServiceConfig) -> None:
    """Raise `LocalTestBypassBindRefusedError` if the bypass is active on a non-loopback host.

    The local-test bypass is unauthenticated, so it must never be reachable from beyond this
    machine. The message states both facts (bypass active, host not loopback) so the operator
    can identify the fix immediately. When the bypass is inactive (the default), this check does
    nothing, leaving the warning-only non-loopback handling in `lifespan()` unchanged.

    Called at two defense-in-depth points: `main()`, before `create_app`/`uvicorn.run` are ever
    reached, and the first statement of `lifespan()`, so a caller that builds `create_app(config)`
    directly is covered too.

    Args:
        config: The resolved service configuration; its bypass flag and `host` are read.

    Raises:
        LocalTestBypassBindRefusedError: If the bypass is active and `config.host` is not loopback.
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
    """The fixed (name, probe) pairs for the dependencies that have a connectivity probe.

    Covers FalkorDB, LLM Interface, Cellar/ELI, Passkey Signing Postgres and PS state Postgres.
    The startup graph replay has no row: its state belongs to the replay task, not to a probe.

    The single source of truth both `_check_dependencies_at_startup` (probes all, unconditionally)
    and the `/readyz` re-probe (which looks a probe up by name and only runs it for an unhealthy
    member of `_GATING_DEPENDENCIES`) build on, so the two never drift apart on which callable
    answers for which dependency name. Passkey Signing
    Postgres's own probe (`check_passkey_signing_postgres_connectivity`) is a no-op when
    `config.passkey_signing_postgres_host` is unset -- unconditionally listed here regardless,
    mirroring how LLM Interface/Cellar/ELI are also always probed even though a deployment may
    leave either unconfigured. PS state Postgres's own probe (`check_state_postgres_connectivity`)
    deliberately does NOT no-op when `config.state_postgres_host` is unset -- unlike Passkey
    Signing, an unconfigured PS state Postgres raises there too, so an unconfigured deployment
    logs a startup warning here and, since `STATE_POSTGRES` is a gating dependency, stays
    `not_ready` rather than silently appearing healthy.

    Args:
        config: The resolved service configuration the probes read their connection settings from.

    Returns:
        One `(dependency name, zero-argument probe)` pair per tracked dependency, in a fixed
        order; calling a probe raises if the dependency is unreachable.
    """
    return (
        (FALKORDB, lambda: check_falkordb_connectivity(config)),
        (LLM_INTERFACE, lambda: check_llm_interface_connectivity(config)),
        (CELLAR_ELI, check_cellar_eli_connectivity),
        (
            PASSKEY_SIGNING_POSTGRES,
            lambda: check_passkey_signing_postgres_connectivity(config),
        ),
        (STATE_POSTGRES, lambda: check_state_postgres_connectivity(config)),
    )


def _apply_passkey_signing_migrations_at_startup(config: ServiceConfig) -> None:
    """Apply any pending Passkey Signing Postgres migrations once at startup.

    Gated on `config.passkey_signing_postgres_host` being configured: Passkey Signing Postgres
    is optional, pilot-scope infrastructure, unlike FalkorDB -- an environment that never
    configures it skips this step entirely rather than failing closed on an absent host.
    Deliberately never raises (mirrors `_check_dependencies_at_startup`): a migration failure is
    logged as a warning and only degrades the one gated merge path this component serves, never
    the whole process's startup.

    Args:
        config: The resolved service configuration; the Passkey Signing Postgres settings are read.

    Side Effects:
        Opens a connection to Passkey Signing Postgres and applies pending schema migrations;
        on any failure writes a `warning` startup log entry carrying the error text.
    """
    if config.passkey_signing_postgres_host is None:
        return
    try:
        with connect_passkey_signing_postgres_for_migrations(config) as conn:
            apply_pending_migrations(conn)
    except Exception as exc:  # noqa: BLE001 - a migration failure must never crash the process (see docstring)
        emit_log_entry(
            component="entrypoint",
            action="startup",
            outcome="warning",
            extra={"dependency": PASSKEY_SIGNING_POSTGRES, "error": str(exc)},
        )


def _apply_state_migrations_at_startup(config: ServiceConfig) -> None:
    """Apply any pending PS state Postgres migrations once at startup, then verify the graph log.

    Skipped, without a connection attempt, when `config.state_postgres_host` is unset: PS state
    Postgres is optional at config-load time, and an unconfigured store keeps the process up but
    out of `/readyz` (its connectivity probe raises when unconfigured and `STATE_POSTGRES` is a
    gating dependency).

    With a host configured, a failure to connect or to apply a migration is FATAL: it is logged
    (exception class only, never its text) and re-raised out of startup, so the process exits and
    the orchestrator restarts it -- a service that cannot bring its own schema up to date must
    never come up looking healthy. This deliberately differs from
    `_apply_passkey_signing_migrations_at_startup`, whose store is optional pilot-scope
    infrastructure whose failure only degrades one gated merge path.

    After the ordinary migrations it verifies, read-only and with the same `ps_state`
    credentials, that the privileged `graph_gateway` and `audit_integrity` migrations (the
    owner-protected graph log tables and the insert-only audit table) were applied by
    `python -m ps_service.graph_gateway.provision`: the service never creates those tables
    itself, so a missing migration, table or schema, or one the application role controls, is
    also FATAL, with a failure entry that names the missing migration.

    Args:
        config: The resolved service configuration; the PS state Postgres settings are read.

    Raises:
        Exception: Whatever the connection, a migration or the privileged migration
            verification raised, including `PrivilegedMigrationMissingError` when a privileged
            migration is missing; the original exception is re-raised after logging.

    Side Effects:
        Opens a connection to PS state Postgres, applies pending ordinary migrations, and on
        failure writes a `failure` startup log entry (see `startup_failure_log_extra`).
    """
    if config.state_postgres_host is None:
        return
    try:
        with connect_state_postgres_for_migrations(config) as conn:
            apply_state_migrations(
                conn,
                sources=ORDINARY_STATE_MIGRATION_SOURCES,
            )
            _verify_privileged_migrations_at_startup(conn)
    except Exception as exc:
        emit_log_entry(
            component="entrypoint",
            action="startup",
            outcome="failure",
            extra=startup_failure_log_extra(exc),
        )
        raise


def _verify_privileged_migrations_at_startup(conn: psycopg.Connection[TupleRow]) -> None:
    """Fail startup unless every owner-protected migration is in place.

    Checks the `graph_gateway` migration (graph log tables) and then the `audit_integrity`
    migration (the insert-only, hash-chained audit table and its schema). One verification per
    source, so a failure names the migration that is actually missing.

    Args:
        conn: An open PS state Postgres connection used for read-only checks.

    Raises:
        PrivilegedMigrationMissingError: If a privileged migration is not recorded, one of its
            tables or schemas is missing, or the application role controls a table or schema.
    """
    verify_privileged_migrations_applied(
        conn,
        sources=[GRAPH_GATEWAY_MIGRATION_SOURCE],
        required_tables=GRAPH_LOG_TABLES,
    )
    verify_privileged_migrations_applied(
        conn,
        sources=[AUDIT_INTEGRITY_MIGRATION_SOURCE],
        required_tables=AUDIT_LOG_TABLES,
        required_schemas=AUDIT_LOG_SCHEMAS,
    )


class _StartupGateway(Protocol):
    """What the lifespan needs of the Graph Write Gateway: replay at startup, stop on teardown.

    The methods are the contract; every implementation may raise on infrastructure failure, and
    the lifespan logs and tolerates that.
    """

    def hold_for_startup_replay(self) -> None:
        """Close every graph to writes until the startup replay has ended.

        Side Effects:
            Every graph refuses writes with a typed error until the replay has dealt with it.
        """
        ...

    def run_startup_replay(self) -> StartupReplayReport:
        """Rebuild the graphs from the mutation log, blocking until done or stopped.

        Returns:
            A report of which graphs were replayed, resumed, caught up, or left gated.

        Raises:
            Exception: If the log store or the graph database stays unavailable until the retrying
                is stopped.

        Side Effects:
            Reads the mutation log, rebuilds, resumes or catches up graphs, and retries through an
            infrastructure outage until it ends or `stop_replay` is called.
        """
        ...

    def gated_graphs(self) -> tuple[str, ...]:
        """Return the names of the graphs that refuse writes until an operator acts.

        Returns:
            The sorted names of graphs whose replay failed or whose logged entry the graph database
            refuses; empty when none.
        """
        ...

    def stop_replay(self) -> None:
        """Ask the running replay to stop at its next page; does not wait for it.

        A stopped replay leaves its progress record and keeps its graph closed to writes.

        Side Effects:
            Sets the stop request the replay checks between pages and while waiting to retry.
        """
        ...

    def stop_reconciler(self, timeout: float) -> bool:
        """Stop the background reconciler, waiting at most `timeout` seconds.

        Args:
            timeout: The longest time to wait for the reconciler to end, in seconds.

        Returns:
            `True` if the reconciler ended within `timeout` or was not running; `False` if it was
            still running.

        Side Effects:
            Signals the reconciler thread to end and joins it.
        """
        ...


@runtime_checkable
class _DriftMonitored(Protocol):
    """What the lifespan needs of a gateway that checks its graphs for drift.

    A gateway that does not implement these methods simply has no drift check.
    """

    def start_drift_monitor(self, interval_seconds: float) -> bool:
        """Start the periodic drift check.

        Args:
            interval_seconds: Seconds between checks; `0` disables the check.

        Returns:
            `True` if a check was started; `False` if `interval_seconds` is `0` or one already
            runs.

        Side Effects:
            Starts a background thread that compares each graph with the checkpoint at the head of
            its log and logs the finding.
        """
        ...

    def stop_drift_monitor(self, timeout: float) -> bool:
        """Stop the periodic drift check, waiting at most `timeout` seconds.

        Args:
            timeout: The longest time to wait for the check to end, in seconds.

        Returns:
            `True` if the check ended within `timeout` or was never started; `False` if it was
            still running.

        Side Effects:
            Signals the drift monitor thread to end and joins it.
        """
        ...


async def _start_graph_replay_at_startup(
    config: ServiceConfig,
    build: Callable[[ServiceConfig], _StartupGateway] | None = None,
) -> tuple[_StartupGateway | None, asyncio.Task[None] | None]:
    """Build the Graph Write Gateway and start replaying the graph log in the background.

    Skipped, returning `(None, None)`, when `config.state_postgres_host` is unset (no log to
    replay) or the gateway cannot be built (logged with the exception class only; startup goes
    on). Otherwise, before this returns (so before any request is served): every graph is closed
    to writes (`hold_for_startup_replay`), the `graph_replay` dependency is marked unhealthy, which
    holds `/readyz` at `not_ready`, and the replay is started as a task whose completion marks it
    healthy again. The replay is blocking, so it runs on a worker thread; startup does not wait
    for it. The ingestion pipelines write through it.

    Args:
        config: The resolved service configuration; `state_postgres_host` and the drift check
            interval are read.
        build: Factory that builds the gateway from `config`; `None` uses the default gateway.

    Returns:
        `(gateway, replay_task)` when the replay was started; `(None, None)` when skipped.

    Raises:
        Exception: Whatever `hold_for_startup_replay` raises.

    Side Effects:
        Installs the gateway as the process-wide write gateway, marks `graph_replay` unhealthy and
        schedules the replay task on the running event loop; a build failure writes a `failure`
        log entry.
    """
    if config.state_postgres_host is None:
        return None, None
    try:
        gateway = (build or build_default_graph_write_gateway)(config)
    except Exception as exc:  # noqa: BLE001 - startup replay must never crash startup (see docstring)
        _log_graph_gateway_startup_replay("failure", {"reason": type(exc).__name__})
        return None, None
    gateway.hold_for_startup_replay()
    # The pipelines that run outside a request (MCP tools, the amendment sweep) reach the one
    # gateway of the process through this handle.
    install_graph_write_gateway(cast("GroupSubmitter", gateway))
    mark_unhealthy(
        GRAPH_REPLAY, error=RuntimeError("the startup replay of the graph log is running")
    )
    return gateway, asyncio.create_task(
        _run_startup_replay(gateway, config.graph_drift_check_interval_seconds)
    )


async def _run_startup_replay(gateway: _StartupGateway, drift_interval_seconds: int = 0) -> None:
    """Run the startup replay on a worker thread; mark `graph_replay` healthy once it has ended.

    When the replay ended, a gateway that checks for drift starts its periodic check: never
    earlier, so no graph is compared with its checkpoint while it is being rebuilt. A replay
    that failed starts no check.

    Ending includes graphs that stay closed because their own replay failed: the others are
    unaffected and `/readyz` goes green (the failure is logged and the graph refuses writes with a
    typed error). An unexpected exception (infrastructure down, a bug) leaves `graph_replay`
    unhealthy, so the pod stays out of rotation, and is logged with its class only.

    Args:
        gateway: The gateway whose startup replay is run.
        drift_interval_seconds: Seconds between drift checks started after the replay; `0`
            starts none.

    Side Effects:
        Runs the replay, updates the `graph_replay` health record, may start the drift monitor
        and writes one startup-replay log entry. Never raises.
    """
    try:
        report = await run_in_threadpool(gateway.run_startup_replay)
    except Exception as exc:  # noqa: BLE001 - a replay failure must be logged, never crash the loop
        _log_graph_gateway_startup_replay("failure", {"reason": type(exc).__name__})
        return
    mark_healthy(GRAPH_REPLAY)
    _start_drift_monitor(gateway, drift_interval_seconds)
    _log_graph_gateway_startup_replay(
        "warning" if report.gated else "success",
        {
            "replayed_graphs": len(report.replayed),
            "resumed_graphs": len(report.resumed),
            "caught_up_graphs": len(report.caught_up),
            "gated_graphs": len(report.gated),
        },
    )


def _start_drift_monitor(gateway: _StartupGateway, interval_seconds: int) -> None:
    """Start the gateway's periodic drift check, if it has one; a failure is logged only."""
    if not isinstance(gateway, _DriftMonitored):
        return
    try:
        gateway.start_drift_monitor(interval_seconds)
    except Exception as exc:  # noqa: BLE001 - the drift check is internal; it must never break startup
        _log_graph_gateway_startup_replay("failure", {"reason": type(exc).__name__})


def _log_graph_gateway_startup_replay(outcome: str, extra: dict[str, object]) -> None:
    emit_log_entry(
        component="entrypoint", action="graph_gateway_startup_replay", outcome=outcome, extra=extra
    )


def _is_ready(app: FastAPI) -> bool:
    """Whether the readiness probe reports ready for the state recorded now.

    Reads only `app.state` and the dependency health registry; writes nothing. A complete
    configuration with every gating dependency healthy counts as ready even before the probe has
    latched `app.state.ready`, because that latch would set it from the same registry.
    """
    may_be_ready = bool(app.state.ready) or bool(app.state.config_complete)
    return may_be_ready and all_healthy(_GATING_DEPENDENCIES)


def _unhealthy_ready_dependencies() -> list[str]:
    """Every `_READY_DEPENDENCIES` member recorded unhealthy, gating or not, in that order."""
    return [dependency for dependency in _READY_DEPENDENCIES if not is_healthy(dependency)]


class _HarnessReadiness:
    """The recorded readiness of one app, as the REST `/status` handler reads it."""

    def __init__(self, app: FastAPI) -> None:
        """Read the readiness of `app`.

        Args:
            app: The app whose `app.state` and gateway are read.
        """
        self._app = app

    def status(self) -> Literal["ready", "not_ready"]:
        """`"ready"` when the readiness probe would report ready for the recorded state."""
        return "ready" if _is_ready(self._app) else "not_ready"

    def unhealthy_dependencies(self) -> list[str]:
        """The tracked dependencies recorded unhealthy, gating or not."""
        return _unhealthy_ready_dependencies()

    def gated_graphs(self) -> list[str]:
        """The graphs that refuse writes until an operator acts."""
        return _gated_graphs(self._app)


def _publish_status_sources(app: FastAPI, verifier: PsTokenVerifier | None) -> None:
    """Publish what the REST `/status` handler reads: the verifier and the recorded readiness.

    Side Effects:
        Sets `app.state.token_verifier` and `app.state.recorded_readiness`.
    """
    app.state.token_verifier = verifier
    app.state.recorded_readiness = _HarnessReadiness(app)


def _gated_graphs(app: FastAPI) -> list[str]:
    """Names of the graphs that refuse writes until someone acts; empty without a gateway."""
    gateway = cast("_StartupGateway | None", getattr(app.state, "graph_write_gateway", None))
    return list(gateway.gated_graphs()) if gateway is not None else []


async def _run_stop_within(stop: Callable[[float], bool], wait_seconds: float) -> bool:
    """Run a blocking gateway stop on its own daemon thread and wait at most `wait_seconds`.

    The stop is passed `wait_seconds` as its own timeout. A stop that has not returned in time
    is left to finish on its thread, which is a daemon so it never holds up process exit, and
    counts as not stopped. If it raises once nobody waits for it any more, the failure is logged
    by its exception class only, from the loop while it is open and from the thread otherwise.

    Args:
        stop: The blocking stop, called with `wait_seconds` as its timeout; it returns whether
            the stopped work ended.
        wait_seconds: The longest time to wait for `stop` to return.

    Returns:
        What `stop` returned, when it returned within `wait_seconds`; `False` otherwise.

    Raises:
        Exception: Whatever `stop` raised, when it raised within `wait_seconds`.
    """
    loop = asyncio.get_running_loop()
    outcome: asyncio.Future[bool] = loop.create_future()
    threading.Thread(
        target=_call_stop_and_hand_over,
        args=(stop, wait_seconds, loop, outcome),
        name="ps-service-shutdown-stop",
        daemon=True,
    ).start()
    finished, _ = await asyncio.wait({outcome}, timeout=wait_seconds)
    if not finished:
        outcome.cancel()
        return False
    return outcome.result()


def _call_stop_and_hand_over(
    stop: Callable[[float], bool],
    wait_seconds: float,
    loop: asyncio.AbstractEventLoop,
    outcome: asyncio.Future[bool],
) -> None:
    """Call `stop` on this thread and hand what it returned or raised to `outcome` on `loop`.

    A failure is logged by its exception class from here when `loop` has already closed.
    """
    result, error = False, None
    try:
        result = stop(wait_seconds)
    except Exception as exc:  # noqa: BLE001 - handed to the awaiting coroutine, which raises it
        error = exc
    try:
        loop.call_soon_threadsafe(_deliver_stop_outcome, outcome, result, error)
    except RuntimeError:
        # The loop has closed, so nobody is waiting any more.
        if error is not None:
            _log_late_stop_failure(error)


def _deliver_stop_outcome(
    outcome: asyncio.Future[bool],
    result: bool,  # noqa: FBT001 - loop callback
    error: Exception | None,
) -> None:
    """Settle `outcome` with a stop's result or error; log an error nobody waits for any more."""
    if outcome.done():
        if error is not None:
            _log_late_stop_failure(error)
        return
    if error is not None:
        outcome.set_exception(error)
    else:
        outcome.set_result(result)


def _log_late_stop_failure(error: Exception) -> None:
    """Log a stop that raised after its wait expired, by its exception class only."""
    _log_graph_gateway_startup_replay("failure", {"reason": type(error).__name__})


def _remaining_seconds(deadline: float, clock: Callable[[], float]) -> float:
    """Seconds left until `deadline` on `clock`; never negative."""
    return max(0.0, deadline - clock())


def _log_not_stopped(reason: str, wait_seconds: float) -> None:
    """Log that a shutdown stop did not end within `wait_seconds`, under `reason`."""
    _log_graph_gateway_startup_replay(
        "warning", {"reason": reason, "timeout_seconds": round(wait_seconds, 3)}
    )


async def _stop_graph_replay(
    gateway: _StartupGateway | None, task: asyncio.Task[None] | None, wait_seconds: float
) -> None:
    """Ask the startup replay to stop at its next page and wait at most `wait_seconds` for it.

    Never raises: a failing stop request is logged by its exception class, and the replay is
    still waited for, since it may end on its own.
    """
    if gateway is None or task is None:
        return
    try:
        gateway.stop_replay()
    except Exception as exc:  # noqa: BLE001 - shutdown must go on whatever the replay stop does
        _log_graph_gateway_startup_replay("failure", {"reason": type(exc).__name__})
    _, still_running = await asyncio.wait({task}, timeout=wait_seconds)
    if still_running:
        _log_not_stopped("replay_not_stopped", wait_seconds)


async def _shutdown_graph_gateway(
    gateway: _StartupGateway | None,
    replay_task: asyncio.Task[None] | None,
    *,
    budget_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Stop the drift check, the startup replay (it holds the graphs) and the reconciler.

    They stop in that order on one deadline `budget_seconds` from now. Each stop is given the
    time left on it, which is 0 once the budget is spent, so a later stop is still signalled.
    Never raises.
    """
    install_graph_write_gateway(None)
    deadline = clock() + budget_seconds
    await _stop_drift_monitor(gateway, _remaining_seconds(deadline, clock))
    await _stop_graph_replay(gateway, replay_task, _remaining_seconds(deadline, clock))
    await _stop_graph_gateway_reconciler(gateway, _remaining_seconds(deadline, clock))


async def _stop_drift_monitor(gateway: _StartupGateway | None, wait_seconds: float) -> None:
    """Stop the gateway's periodic drift check at shutdown; never raises, waits `wait_seconds`."""
    if not isinstance(gateway, _DriftMonitored):
        return
    try:
        stopped = await _run_stop_within(gateway.stop_drift_monitor, wait_seconds)
    except Exception as exc:  # noqa: BLE001 - shutdown must go on whatever the drift check does
        _log_graph_gateway_startup_replay("failure", {"reason": type(exc).__name__})
        return
    if not stopped:
        _log_not_stopped("drift_monitor_not_stopped", wait_seconds)


async def _stop_graph_gateway_reconciler(
    gateway: _StartupGateway | None, wait_seconds: float
) -> None:
    """Stop the gateway's background reconciler at shutdown; never raises, waits `wait_seconds`."""
    if gateway is None:
        return
    try:
        stopped = await _run_stop_within(gateway.stop_reconciler, wait_seconds)
    except Exception as exc:  # noqa: BLE001 - shutdown must go on whatever the reconciler does
        _log_graph_gateway_startup_replay("failure", {"reason": type(exc).__name__})
        return
    if not stopped:
        _log_not_stopped("reconciler_not_stopped", wait_seconds)


def startup_failure_log_extra(exc: Exception) -> dict[str, object]:
    """Return the `extra` of a fatal state-Postgres startup failure: class names only.

    Never the exception text (it can carry hosts or driver detail). A missing privileged
    migration also names the migration and the fixed reason, the facts an operator acts on.

    Args:
        exc: The exception that failed PS state Postgres startup.

    Returns:
        A mapping with `dependency` (the state Postgres name) and `reason` (the exception class
        name); for a `PrivilegedMigrationMissingError` also `missing_migration` and
        `missing_reason`.
    """
    extra: dict[str, object] = {"dependency": STATE_POSTGRES, "reason": type(exc).__name__}
    if isinstance(exc, PrivilegedMigrationMissingError):
        extra["missing_migration"] = exc.missing_migration
        extra["missing_reason"] = exc.reason
    return extra


def _check_dependencies_at_startup(config: ServiceConfig) -> bool:
    """Probe every tracked dependency once at startup, logging a warning per failure.

    Returns whether every `_GATING_DEPENDENCIES` member is healthy after the probes -- the only
    outcome that gates `app.state.ready`. Non-gating dependencies (LLM Interface, Cellar/ELI) are
    still probed unconditionally, in the same fixed order, and a failure in either is still
    logged, but they never affect the return value. The recorded readiness `/status` reports to a
    verified caller still names either via `unhealthy_dependencies`.

    Deliberately never raises: unlike `configure()`'s failures, a dependency outage must never
    crash the process, only keep it out of `/readyz`'s pool. Runs every probe even after an
    earlier one fails, so a single startup gives the full picture of what is down rather than
    stopping at the first failure.

    Each probe also records its outcome in `ps_service.dependency_health`
    (`falkordb_client.check_connectivity_from_config`, `llm_interface.check_connectivity`,
    `cellar_eli.fetch.check_connectivity` all do this themselves). That registry is what
    `all_healthy(_GATING_DEPENDENCIES)` reads back here and what `/readyz` keeps reading after
    startup, where a gating dependency that fails later is re-probed on each poll.

    Args:
        config: The resolved service configuration the probes read their connection settings from.

    Returns:
        `True` if every gating dependency is healthy after probing; `False` otherwise.

    Side Effects:
        Calls each dependency, updates the dependency health registry and writes a `warning`
        startup log entry (with the error text) per failed probe.
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


async def _refresh_readiness(app: FastAPI, reprobe: GatingReprobe, config: ServiceConfig) -> None:
    """Run the `/readyz` re-probe under its deadline, then latch `app.state.ready` if it may.

    The deadline is `config.ready_reprobe_deadline_seconds` from the start of this call, so the
    whole poll stays bounded however slow a dependency is. The re-probe runs only while
    `app.state.config_complete` is `True`: missing configuration cannot be cured by a dependency
    recovering. When the deadline passes first, the re-probe is cancelled (a probe thread keeps
    running and keeps its slot). Afterwards, if `app.state.ready` is `False` and the
    configuration is complete, it is set from the registry. It is never reset to `False`.

    Args:
        app: The app whose readiness state is read and, at most, latched `True`.
        reprobe: The re-probe harness for the gating dependencies.
        config: Supplies the deadline.

    Side Effects:
        May start probe threads that call dependencies and update the dependency health registry;
        may set `app.state.ready`.
    """
    if not app.state.config_complete:
        return
    loop = asyncio.get_running_loop()
    deadline_at = loop.time() + config.ready_reprobe_deadline_seconds
    refresh = loop.create_task(reprobe.refresh(deadline_at))
    try:
        await asyncio.wait({refresh}, timeout=max(0.0, deadline_at - loop.time()))
    finally:
        refresh.cancel()
    if not app.state.ready:
        app.state.ready = all_healthy(_GATING_DEPENDENCIES)


def _build_app_metrics(
    config: ServiceConfig, pending_entries: Callable[[], int] | None
) -> MetricsHandle:
    """Build the app's metrics handle with its data sources wired.

    The unhealthy-dependency gauge reads the dependency-health registry. The pending graph-log
    gauge reads `pending_entries` through a `TtlCachedSource` (cached, single-flight, with one
    deadline), so a scrape never waits on Postgres for long and a down Postgres is not probed by a
    scrape loop.

    Args:
        config: Supplies the PS state Postgres coordinates for the default pending-entries source.
        pending_entries: Returns the pending graph-log total; `None` reads it from PS state
            Postgres with `count_pending_entries`.

    Returns:
        The new handle.

    Side Effects:
        As `build_metrics`.
    """
    fetch = pending_entries or functools.partial(
        count_pending_entries,
        config,
        connect_timeout_s=PENDING_CONNECT_TIMEOUT_S,
        statement_timeout_ms=PENDING_STATEMENT_TIMEOUT_MS,
    )
    pending = TtlCachedSource(
        "graph_log_pending_entries",
        fetch,
        ttl_seconds=PENDING_TTL_S,
        timeout_seconds=PENDING_SOURCE_DEADLINE_S,
    )
    return build_metrics(unhealthy_counts=_unhealthy_dependency_counts, pending_entries=pending.get)


def _add_http_middleware(
    app: FastAPI,
    verifier: PsTokenVerifier | None,
    auth_context: AuthContext | None,
    max_request_body_bytes: int,
    metrics: MetricsHandle,
    public_url: str | None,
) -> None:
    """Wire the four HTTP middleware layers; the last one added is the outermost.

    Args:
        app: The application to add the middleware to.
        verifier: Bearer-token verifier for `RestAuthMiddleware`; `None` when the local-test
            bypass is active.
        auth_context: Resolved authentication settings for `RestAuthMiddleware`; `None` when the
            bypass is active.
        max_request_body_bytes: The largest accepted `Content-Length`, in bytes.
        metrics: The app's metrics handle; its request-duration instrument feeds
            `MetricsMiddleware`.
        public_url: The configured public origin the 401 challenge names, or `None`.
    """
    # `RestAuthMiddleware` is added *before* `_MaxBodySizeMiddleware` so the latter runs first of
    # the two: `add_middleware` makes the most-recently-added call the outermost, so an oversized
    # request gets a cheap 413 on its `Content-Length` header before any JWT/RSA verification work
    # happens -- there is no data dependency the other way (the size check never reads
    # `Authorization`), so this ordering costs nothing and avoids wasted verify work.
    app.add_middleware(
        RestAuthMiddleware, verifier=verifier, auth_context=auth_context, public_url=public_url
    )
    app.add_middleware(_MaxBodySizeMiddleware, max_bytes=max_request_body_bytes)
    # Added last, so outermost: a gated request is answered before any size check or JWT work,
    # and tells an unauthenticated caller nothing beyond "not ready".
    app.add_middleware(_ReplayReadGateMiddleware)
    # Added after the gate, so outermost of all: the gate's 503, the size check's 413 and the
    # authentication 401 are counted too, each under its route template (or the fixed unmatched
    # value), never the raw path.
    app.add_middleware(
        MetricsMiddleware,
        histogram=metrics.histogram(REQUEST_DURATION),
        router=app.router,
    )


def _wire_http_surface(app: FastAPI) -> None:
    """Install the exception handlers and include the REST, signing-ceremony and `/metrics` routers.

    Args:
        app: The application to wire; `app.state.metrics` must already be set for `/metrics`.

    Side Effects:
        Registers the structured error handlers and the three routers on `app`.
    """
    register_exception_handlers(app)
    app.include_router(build_api_router())
    app.include_router(build_passkey_signing_router())
    app.include_router(build_metrics_router())


def _emit_startup_warnings(config: ServiceConfig) -> None:
    """Log one warning for each unsafe-looking setting the process starts with.

    Covers a non-loopback bind, an active local-test bypass, and an exposed deployment that
    configures no Passkey Signing allowed origins (so the relying party follows each request's
    `Host`). Nothing is raised; the entries only make the condition visible to an operator.
    """
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
    elif not config.passkey_signing_allowed_origins:
        emit_log_entry(
            component="passkey_signing",
            action="allowed_origins_check",
            outcome="warning",
            extra={"allowed_origins_configured": False},
        )


def _require_complete_startup_config(config: ServiceConfig) -> None:
    """Fail closed on any startup configuration the process cannot run without.

    Raises:
        AccessRoleBootstrapConfigurationError: Unless the local-test bypass is active or both
            `authz_bootstrap_owner_subject`/`_issuer` are set.
        AuthentikCredentialConfigurationError: Unless both `authentik_api_token`/
            `authentik_base_url` are set, regardless of the bypass.
        AuthConfigurationError: Unless the local-test bypass is active or `public_url` is set.
    """
    require_bootstrap_owner_configured(config)
    require_authentik_credential_configured(config)
    require_public_url_configured(config)


def create_app(
    config: ServiceConfig, *, pending_entries: Callable[[], int] | None = None
) -> FastAPI:
    """Build the FastAPI app wired to the given `ServiceConfig`.

    A factory rather than a module-level singleton so that two independently configured apps can
    coexist without sharing mutable state (e.g. two `TestClient`s in one test file): `lifespan`
    is a closure capturing `config`, and the readiness flag lives on `app.state` rather than a
    module global. `config.logging_dir` (joined with the fixed log filename) is threaded
    explicitly into the Logging facade, so two apps with different `logging_dir`s never leak into
    each other's log sink.

    The resolved `config` is stashed on `app.state.config` (read back by the REST layer's
    `get_service_config` dependency). `register_exception_handlers(app)` installs the structured
    4xx/5xx error handlers, which do not leak stack traces, paths or infrastructure detail. The
    `ps_service.api` REST router and the companion-browser signing-ceremony router
    (`/approvals/*`, exempt from bearer authentication because it is gated by its own per-request
    `code` check) are mounted with `app.include_router`, as is the anonymous `GET /metrics`
    scrape route (`ps_service.metrics`), whose `MetricsHandle` is stashed on `app.state.metrics`
    and shut down with the app. The in-cluster probes `/livez`, `/readyz` and the
    `/.well-known/oauth-protected-resource` metadata (RFC 9728, the URL named in
    `RestAuthMiddleware`'s `WWW-Authenticate` header) are registered directly. The metadata route
    is the only one publishing that document: the MCP sub-app leaves its resource server URL
    unset, so the MCP SDK never registers a competing one under `/mcp`.

    MCP Interface's Streamable HTTP ASGI sub-app is mounted unconditionally at
    `MCP_HTTP_MOUNT_PATH` (`/mcp`) in the same process and port, wrapping the one
    `mcp_server.server` singleton. It receives the
    same `verifier` and `auth_context` as `RestAuthMiddleware`, so
    `/mcp` requests pass through the MCP SDK's own token-verifier gate and share the process-wide
    verifier and JWKS cache. Because Starlette does not propagate a mounted sub-app's lifespan,
    `lifespan` enters the sub-app's lifespan context explicitly, so the SDK session manager only
    starts after the bypass-bind check, logging configuration and startup warning entries have
    run (fail-fast ordering).

    Three configuration checks run synchronously here, before the app is returned, because the
    facts they check cannot change for the life of the process:
    `resolve_auth_context(config)` fails closed unless the local-test bypass is active or
    `PS_AUTH_ISSUER` and `PS_AUTH_AUDIENCE` are both set; the resolved (possibly `None`) context
    is stashed on `app.state.auth_context`. `require_bootstrap_owner_configured(config)` fails
    closed unless the bypass is active or `PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT` and
    `PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER` are both set: only the principal with that `(sub, iss)` is
    ever granted `SYSTEM_OWNER` by the access role store's first-owner bootstrap.
    `require_authentik_credential_configured(config)` carries no bypass exemption: it fails
    unless `PS_AUTHENTIK_API_TOKEN` and `PS_AUTHENTIK_BASE_URL` are both set.
    `require_public_url_configured(config)` fails closed unless the bypass is active or
    `PS_PUBLIC_URL` is set. `RestAuthMiddleware`
    is then wired to a `PsTokenVerifier` built from the auth context (or `None` when the bypass
    is active, in which case every request is let through unauthenticated). Every path other than
    `/livez`, `/readyz`, `/status` (whose handler verifies an optional token itself),
    `/.well-known/*`, `/mcp*` (gated by the MCP SDK) and `/approvals/*` requires a verified
    bearer token. The verifier and the recorded readiness are published on `app.state` for the
    REST `/status` handler.

    Args:
        config: The resolved service configuration. It is captured by reference for the life of
            the app; its host, logging directory, request body limit and authentication settings
            are all read from it.
        pending_entries: Test seam for the pending graph-log gauge's data source: a callable
            returning the total, replacing the read from PS state Postgres (it is still cached
            and bounded like the real one). `None` reads PS state Postgres.

    Returns:
        A new, not yet started `FastAPI` app. Its readiness (`app.state.ready`) is `False` until
        the lifespan startup completes and every gating dependency is healthy.

    Raises:
        AuthConfigurationError: If the bypass is inactive and the authentication issuer or
            audience is not configured, or the public URL is not configured.
        AccessRoleBootstrapConfigurationError: If the bypass is inactive and the bootstrap owner
            subject or issuer is not configured.
        AuthentikCredentialConfigurationError: If the Authentik API token or base URL is not
            configured.

    Side Effects:
        Builds the verifier and the MCP sub-app (the verifier fetches discovery and JWKS lazily,
        on first use). Nothing is bound, logged or probed until the app's lifespan starts.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Configure Logging, emit startup entries, probe dependencies once, then flip readiness.

        `configure(log_path=...)` is called before any `emit_log_entry` call, per the Logging
        facade's contract that a process-wide default emitter must be installed first.
        `config.logging_dir` is a *directory* (matching `PS_LOGGING_DIR`'s env-var semantics),
        while `configure()`'s `log_path` is a literal *file* path, so `config.logging_dir` is
        joined with the fixed filename `_LOG_FILENAME` here. Threading this explicitly (rather than
        bare `configure()`) makes readiness and logging configuration both flow from this
        instance's `config`, not from Logging's environment-derived fallback.

        If `config.host` is not loopback, an `outcome="warning"` entry is emitted before the
        unconditional `outcome="success"` entry; if the local-test bypass is active, a further
        `outcome="warning"` entry states both facts an operator needs
        (`local_test_bypass_active=True`, `bind_scope="loopback-only"`), on every process start,
        since nothing here gates on prior-warning state. Both warnings are additive, never
        replacing the success entry. Any exception raised by `configure()` (e.g.
        `LoggingConfigurationError`) is deliberately left to propagate (fail-fast); the uvicorn
        startup-failure path reports it to stderr.

        `app.state.ready` only flips `True` once `_check_dependencies_at_startup` confirms every
        `_GATING_DEPENDENCIES` member is reachable AND every `INGESTION_REQUIRED_CONFIG_FIELDS`
        value resolved. LLM Interface and Cellar/ELI are still probed unconditionally at startup
        and logged on failure, but neither outcome affects this flag: a transient outage at boot
        must not wedge readiness for the rest of the process's life. Unlike `configure()`, neither
        a dependency failure nor incomplete config propagates here: each only keeps this instance
        out of `/readyz`'s pool, so an outage or an incomplete deploy never crash-loops an
        otherwise-healthy process.

        Missing config is checked once here, not folded into `dependency_health`'s live-updating
        registry: `config` is a frozen `ServiceConfig` resolved once by `load_config()`, so unlike
        dependency reachability it cannot change or recover for the life of this process. Its
        result is stashed on `app.state.config_complete` (not only folded into the
        `app.state.ready` assignment) because `readyz()` reads it again on every poll:
        `app.state.ready` can flip `True` after this function returns once a gating dependency
        recovers, and `config_complete` being `False` must keep blocking that forever.

        Args:
            app: The application whose startup and shutdown this lifespan manages.

        Raises:
            LocalTestBypassBindRefusedError: If the local-test bypass is active on a non-loopback
                host.
            LoggingConfigurationError: If the Logging facade cannot be configured.
            Exception: Whatever PS state Postgres migration or graph log verification raised, when
                that store is configured.

        Side Effects:
            Configures the process-wide log sink, writes startup log entries, applies pending
            Passkey Signing and PS state Postgres migrations, starts the graph replay task, enters
            the MCP sub-app lifespan, probes the dependencies, and on shutdown stops the gateway
            and sets `app.state.ready` back to `False`.
        """
        _refuse_non_loopback_bypass_bind(config)
        log_path = (config.logging_dir / _LOG_FILENAME) if config.logging_dir is not None else None
        configure(log_path=log_path, mirror_failures_to_stderr=True)
        _emit_startup_warnings(config)
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
        _apply_passkey_signing_migrations_at_startup(config)
        _apply_state_migrations_at_startup(config)
        # Marks `graph_replay` unhealthy before anything can read readiness, then replays in the
        # background: the server starts, `/readyz` stays `not_ready` until the replay ends.
        app.state.graph_write_gateway, replay_task = await _start_graph_replay_at_startup(config)
        async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
            app.state.ready = _check_dependencies_at_startup(config) and app.state.config_complete
            yield
            await _shutdown_graph_gateway(
                app.state.graph_write_gateway,
                replay_task,
                budget_seconds=config.shutdown_stop_budget_seconds,
            )
            app.state.metrics.shutdown()
            app.state.ready = False

    app = FastAPI(lifespan=lifespan)
    app.state.ready = False
    app.state.config_complete = False
    app.state.config = config
    # Resolved synchronously here, not inside the async `lifespan` closure, so that a bare
    # (never-entered) `TestClient` or ASGI middleware still has a concrete auth decision the
    # moment the app exists.
    auth_context = resolve_auth_context(config)
    app.state.auth_context = auth_context
    # Resolved synchronously here too, right after `resolve_auth_context`, for the same reason:
    # a config-completeness fact that cannot change for the life of the process must fail before
    # `create_app` ever returns an app, not asynchronously after `TestClient`/uvicorn have
    # started.
    _require_complete_startup_config(config)
    # One `PsTokenVerifier` instance per `create_app()` call, never module-level (no import-time
    # side effects) -- this exact instance is what both `RestAuthMiddleware` below and the MCP
    # `token_verifier=` wiring share, so there is one discovery fetch, one `PyJWKClient`, one JWKS
    # cache per process, never two parallel verifiers. The REST `/status` handler reads the same
    # instance through `app.state`.
    verifier = PsTokenVerifier(auth_context) if auth_context is not None else None
    _publish_status_sources(app, verifier)
    metrics = app.state.metrics = _build_app_metrics(config, pending_entries)
    _add_http_middleware(
        app, verifier, auth_context, config.max_request_body_bytes, metrics, config.public_url
    )
    _wire_http_surface(app)

    mcp_asgi_app = build_streamable_http_app(
        server=server, host=config.host, verifier=verifier, auth_context=auth_context
    )
    app.mount(MCP_HTTP_MOUNT_PATH, mcp_asgi_app)

    async def livez() -> dict[str, str]:
        """Report liveness: "alive" as soon as the ASGI server accepts connections.

        Must never depend on `lifespan` startup progress or check external dependencies -- a
        dependency outage must never fail liveness. The orchestrator probes it in-cluster; it is
        not routed through the Ingress, and the client-facing version is served by `/status`.

        Returns:
            `{"status": "alive"}`, always with HTTP 200.
        """
        return {"status": "alive"}

    reprobe = GatingReprobe(
        dependencies=lambda: _GATING_DEPENDENCIES,
        probe_for=dict(_all_dependency_probes(config)).get,
        is_healthy=is_healthy,
        min_interval_seconds=config.ready_reprobe_min_interval_seconds,
    )

    async def readyz() -> JSONResponse:
        """Report "ready" only once startup succeeded AND every gating dependency is healthy now.

        Two independent gates: `app.state.ready` AND the live `dependency_health` registry's
        `_GATING_DEPENDENCIES` entries (read via `all_healthy`) both have to hold. The live gate is
        what lets `/readyz` flip back to `not_ready` if a gating dependency fails mid-run.

        A pod that is not ready receives no traffic, so no real call would ever mark a failed
        dependency healthy again. Every poll therefore re-probes each gating dependency that the
        registry currently records as unhealthy, through the dependency's own connectivity probe
        (which records its outcome itself), off the event loop and under
        `config.ready_reprobe_deadline_seconds` counted from the start of the poll. Concurrent
        polls share one probe per dependency. A healthy dependency, a non-gating one (LLM
        Interface, Cellar/ELI, Passkey Signing Postgres) and the graph replay are never probed from
        here: the LLM probe costs quota, and the replay's state belongs to the replay task. A
        recovered dependency turns the next answer back to `ready`, with no restart.

        `app.state.ready` alone would be a one-time snapshot from `lifespan`'s startup probe, which
        would latch `False` forever if a gating dependency was still down at boot. While it is
        `False`, this handler therefore sets it from the registry once the re-probe has run, so
        a dependency that recovers after a failed startup probe lets the pod become ready.
        `app.state.config_complete` (stashed once in `lifespan`) gates both the re-probe and that
        latch: it cannot change mid-run, so once it is `False` no amount of dependency recovery
        may ever flip `app.state.ready` `True`. Once `app.state.ready` is `True`, this handler
        never resets it (only `lifespan`'s shutdown does); from then on, live degradation is
        caught solely by the `all_healthy(_GATING_DEPENDENCIES)` half. LLM Interface and
        Cellar/ELI are deliberately excluded from `_GATING_DEPENDENCIES`: they are still probed at
        startup and tracked live in `dependency_health`, but neither their startup nor live health
        ever flips `/readyz`'s status.

        The body is `{"status": ...}` alone, whoever asks: the `Authorization` header is never
        read and no token is verified here. The orchestrator probes this route in-cluster; it is
        not routed through the Ingress. The dependency and graph names reach a verified caller
        only through the REST `/status` handler, which reads the same recorded readiness.

        Returns:
            A JSON response `{"status": "ready" | "not_ready"}`. The HTTP status is `200` when
            ready and `503` when not, so the chart's body-blind `httpGet` `readinessProbe` pulls
            the pod from Service rotation on a real outage.

        Side Effects:
            Re-probes the gating dependencies that are recorded unhealthy (the probes update the
            dependency health registry) and, while not yet ready, may set `app.state.ready`.
        """
        await _refresh_readiness(app, reprobe, config)
        is_ready = _is_ready(app)
        return JSONResponse(
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if is_ready else "not_ready"},
        )

    app.add_api_route("/livez", livez, methods=["GET"])
    app.add_api_route("/readyz", readyz, methods=["GET"])
    app.add_api_route(
        "/.well-known/oauth-protected-resource",
        protected_resource_metadata,
        methods=["GET"],
        response_model_exclude_none=True,
    )
    # RFC 9728 path-specific form for the MCP endpoint; not part of the REST surface.
    app.add_api_route(
        "/.well-known/oauth-protected-resource/{resource_path:path}",
        protected_resource_metadata_for_path,
        methods=["GET"],
        response_model_exclude_none=True,
        include_in_schema=False,
    )

    return app


def main() -> None:
    """Resolve configuration once, build the app, and run it under uvicorn until it is stopped.

    This is the process's composition root: `load_config()` is called exactly once, resolving the
    full `PS_SERVICE_*`/`PS_LOGGING_DIR` config surface, and the resulting `ServiceConfig` is
    injected explicitly into both `create_app()` and `uvicorn.run()` -- no component downstream
    reads `os.environ` independently. SIGTERM/SIGINT handling is delegated entirely to uvicorn's
    built-in signal handling, with no custom `asyncio` signal handling here.

    Raises:
        ServiceConfigurationError: If the environment does not resolve to a valid `ServiceConfig`.
        LocalTestBypassBindRefusedError: If the local-test bypass is active on a non-loopback
            host.
        AuthConfigurationError: If authentication is not configured and the bypass is inactive.
        AccessRoleBootstrapConfigurationError: If the bootstrap owner is not configured and the
            bypass is inactive.
        AuthentikCredentialConfigurationError: If the Authentik credential is not configured.

    Side Effects:
        Reads the process environment, binds the configured host and port and serves requests
        until uvicorn receives a termination signal.
    """
    config = load_config()
    _refuse_non_loopback_bypass_bind(config)
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        timeout_graceful_shutdown=config.graceful_shutdown_seconds,
        # Behind the Ingress the pod only sees plain HTTP from the proxy's pod IP. Trusting its
        # X-Forwarded-Proto makes request.url.scheme `https`, which the protected-resource
        # metadata advertises. Only the scheme/client address are affected.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
