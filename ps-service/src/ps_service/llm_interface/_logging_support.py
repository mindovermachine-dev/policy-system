"""Shared `_log` helper for `ps_service.llm_interface`'s two action modules
(`completion.py`, `embedding.py`) — factored out once both needed the
identical duration/outcome/model logging shape (L1/L2 DRY: extract once a
pattern repeats a third time — `route_completion`, `route_embedding`, and
this module's own definition).
"""

from __future__ import annotations

import time

from ps_service.logging.emitter import LogEmitter
from ps_service.logging.facade import emit_log_entry


def _log(*, action: str, outcome: str, started: float, model: str, emitter: LogEmitter | None) -> None:
    """Emit a `LogEntry` for a completed RouteCompletion/RouteEmbedding call.

    Only ever logs the model-id string via `extra={"model": model}` — never
    prompt/completion/embedding content (L1 "never log secrets... or PII").
    `run_id` is never passed explicitly, so `emit_log_entry` auto-bakes the
    currently bound run context — the mechanism AC-004/AC-005 rely on.
    """
    duration_ms = (time.perf_counter() - started) * 1000
    emit_log_entry(
        component="llm_interface",
        action=action,
        outcome=outcome,
        duration_ms=duration_ms,
        extra={"model": model},
        emitter=emitter,
    )
