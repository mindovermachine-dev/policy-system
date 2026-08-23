"""PS Service process harness: FastAPI app exposing liveness/readiness only.

Deliberately decoupled from `ps_service/api/`'s REST layer. It only proves
the process boots and reports its own health.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ps_service.config import ServiceConfig, load_config
from ps_service.logging.facade import configure, emit_log_entry

_LOG_FILENAME = "ps-service.jsonl"  # matches ps_service/logging/facade.py's _DEFAULT_LOG_FILENAME and
# docs/architecture/ps-service-container-architecture.md's documented sink filename — kept as a local
# literal (not imported) so this fix stays entirely within main.py, without touching the already-shipped,
# already-reviewed Logging component's private API surface.

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback(host: str) -> bool:
    """Return whether `host` is one of the recognized loopback spellings.

    Exact string match only against the three spellings a developer/operator
    would plausibly type for "this machine only" (IPv4 loopback literal, the
    conventional hostname, IPv6 loopback literal) — deliberately not full
    `127.0.0.0/8` range matching, which would need `ipaddress` parsing for
    marginal benefit no AC asks for.
    """
    return host in _LOOPBACK_HOSTS


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
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Configure Logging with this app's `config`, emit startup log entries, then flip the readiness flag.

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
        never replaces the success entry. Any exception raised here (e.g.
        `LoggingConfigurationError`) is deliberately left to propagate —
        fail-fast (L1) — rather than swallowed. Uvicorn's own
        startup-failure path reports it to stderr.
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
        app.state.ready = True
        yield
        app.state.ready = False

    app = FastAPI(lifespan=lifespan)
    app.state.ready = False

    async def health() -> dict[str, str]:
        """Report liveness: "alive" as soon as the ASGI server accepts connections.

        Must never depend on `lifespan` startup progress or check external
        dependencies — a dependency outage must never fail liveness.
        """
        return {"status": "alive"}

    async def ready() -> dict[str, str]:
        """Report readiness: "ready" only once `lifespan` startup completes."""
        return {"status": "ready" if app.state.ready else "not_ready"}

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
