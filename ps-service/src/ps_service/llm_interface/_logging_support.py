"""Shared logging helper for the `ps_service.llm_interface` routing modules.

Purpose: give the completion and embedding routing functions one identical
duration/outcome/model log-entry shape instead of repeating it in each module.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from ps_service.logging.emitter import LogEmitter


def log(
    *, action: str, outcome: str, started: float, model: str, emitter: LogEmitter | None
) -> None:
    """Emit one log entry describing a finished LLM Interface call.

    Only the model-id string is logged (as `extra["model"]`); prompt, completion and
    embedding content are never logged, to keep secrets and personal data out of logs.
    `run_id` is never passed explicitly, so the entry carries whatever run context is
    currently bound on the calling thread.

    Args:
        action: Name of the operation that ran, for example `"route_completion"`.
        outcome: Result label recorded on the entry, for example `"success"` or `"error"`.
        started: A `time.perf_counter()` reading taken before the call began; the logged
            duration in milliseconds is the time elapsed since it.
        model: Model identifier sent to the provider; logged verbatim.
        emitter: Log emitter to write to, or None to use the process default emitter.

    Returns:
        None.

    Raises:
        LoggingLifecycleError: `emitter` is None and no process default emitter has been
            configured.

    Side Effects:
        Enqueues one log entry (component `llm_interface`) for asynchronous writing.
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
