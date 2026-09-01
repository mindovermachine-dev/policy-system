"""PS Service process harness and composition root: the FastAPI app.

Owns liveness/readiness (`/health`, `/ready`) and, since issue #51, mounts the
`ps_service.api` REST router into `create_app` (the one deliberate
`ps_service.api` import). It still has no module-load import of, and no
readiness relationship with, Domain Mapper, Company Merge, Query Engine, MCP
Interface, or Regulatory Change Monitor.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI

from ps_service.api.error_handlers import register_exception_handlers
from ps_service.api.routes import build_api_router
from ps_service.config import ServiceConfig, load_config, missing_ingestion_config_fields
from ps_service.dependency_health import (
    CELLAR_ELI,
    FALKORDB,
    LLM_INTERFACE,
    all_healthy,
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

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# Matches `_DEFAULT_LOG_FILENAME` in `ps_service/logging/facade.py` and the sink filename documented
# in `docs/architecture/ps-service-container-architecture.md`. Kept as a local literal (not
# imported) so this fix stays within main.py, without touching the already-shipped, already-reviewed
# Logging component's private API surface.
_LOG_FILENAME = "ps-service.jsonl"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_READY_DEPENDENCIES = (FALKORDB, LLM_INTERFACE, CELLAR_ELI)


def _is_loopback(host: str) -> bool:
    """Return whether `host` is one of the recognized loopback spellings.

    Exact string match only against the three spellings a developer/operator
    would plausibly type for "this machine only" (IPv4 loopback literal, the
    conventional hostname, IPv6 loopback literal) — deliberately not full
    `127.0.0.0/8` range matching, which would need `ipaddress` parsing for
    marginal benefit no AC asks for.
    """
    return host in _LOOPBACK_HOSTS


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
        `outcome="success"` entry (AC-BI-011) — the warning is additive, it
        never replaces the success entry. Any exception raised by
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
        log_path = (config.logging_dir / _LOG_FILENAME) if config.logging_dir is not None else None
        configure(log_path=log_path)
        if not _is_loopback(config.host):
            emit_log_entry(
                component="entrypoint",
                action="startup",
                outcome="warning",
                extra={"host": config.host},
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
        app.state.ready = _check_dependencies_at_startup(config) and not missing_config
        yield
        app.state.ready = False

    app = FastAPI(lifespan=lifespan)
    app.state.ready = False
    app.state.config = config
    register_exception_handlers(app)
    app.include_router(build_api_router())

    async def health() -> dict[str, str]:
        """Report liveness: "alive" as soon as the ASGI server accepts connections.

        Must never depend on `lifespan` startup progress or check external
        dependencies — a dependency outage must never fail liveness.
        """
        return {"status": "alive"}

    async def ready() -> dict[str, str]:
        """Report "ready" only once startup succeeded AND every dependency is currently healthy.

        Two independent gates (issue #22): `app.state.ready` (the one-time
        startup probe from `lifespan` — which itself folds in both the three
        dependency probes AND ingestion config completeness, issue #16
        follow-up) AND `dependency_health.all_healthy(...)` (the live signal,
        updated by real FalkorDB/LLM Interface/Cellar-ELI traffic as it
        happens) both have to hold. The live gate is what lets `/ready` flip
        back to `not_ready` if a dependency fails mid-run, and self-heal on
        its next success, without waiting for a restart — config
        completeness has no equivalent live gate because it cannot change
        mid-run (see `lifespan`'s docstring), so `app.state.ready` alone is
        the whole story for that half.
        """
        is_ready = app.state.ready and all_healthy(_READY_DEPENDENCIES)
        return {"status": "ready" if is_ready else "not_ready"}

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
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        timeout_graceful_shutdown=config.graceful_shutdown_seconds,
    )
