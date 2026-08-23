"""PS Service process harness: FastAPI app exposing liveness/readiness only.

Deliberately decoupled from `ps_service/api/`'s REST layer. It only proves
the process boots and reports its own health.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ps_service.logging.facade import configure, emit_log_entry

HOST = "127.0.0.1"
PORT = 8000
TIMEOUT_GRACEFUL_SHUTDOWN_SECONDS = 10

_ready = False  # module-level, process-local readiness flag (single-worker assumption)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configure Logging, emit a startup log entry, then flip the readiness flag.

    `configure()` is called before `emit_log_entry`, per the
    Logging facade's contract that a process-wide default emitter must be
    installed before any `emit_log_entry` call. Any exception raised here
    (e.g. `LoggingConfigurationError`) is deliberately left to propagate —
    fail-fast (L1) — rather than swallowed. Uvicorn's own startup-failure path reports it to stderr.
    """
    global _ready
    configure()
    emit_log_entry(component="entrypoint", action="startup", outcome="success")
    _ready = True
    yield
    _ready = False


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Report liveness: "alive" as soon as the ASGI server accepts connections.

    Must never depend on `lifespan` startup progress or check external
    dependencies — a dependency outage must never fail liveness.
    """
    return {"status": "alive"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    """Report readiness: "ready" only once `lifespan` startup completes."""
    return {"status": "ready" if _ready else "not_ready"}


def main() -> None:
    """Run the FastAPI app under uvicorn, bound to localhost with an explicit graceful-shutdown timeout.

    SIGTERM/SIGINT handling is delegated entirely to uvicorn's built-in
    signal handling — no custom `asyncio` signal handling here, per the L2
    coding standard's Entrypoint / Process Lifecycle Patterns section.
    """
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        timeout_graceful_shutdown=TIMEOUT_GRACEFUL_SHUTDOWN_SECONDS,
    )
