"""Process-wide entry point for the Logging component's public actions.

`bind_run_context`/`current_run_id` live in run_context.py directly (no
process state needed). This module owns the one piece of process-wide state
the component has: the default `LogEmitter` that `configure()` installs (L2:
"a process-wide default emitter is acceptable as infrastructure, but every
test must be able to substitute its own `LogEmitter`" — which `emit_log_entry`'s
`emitter=` parameter provides).
"""

from __future__ import annotations

import atexit
import contextlib
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from ps_service.logging.emitter import EmitterConfig, LogEmitter, TextSink
from ps_service.logging.errors import LoggingConfigurationError, LoggingLifecycleError
from ps_service.logging.models import EntityId, LogEntry
from ps_service.logging.run_context import current_run_id

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGING_DIR_ENV_VAR = "PS_LOGGING_DIR"  # L2 env naming: PS_<COMPONENT>_<SETTING>
_DEFAULT_LOG_DIRNAME = "logs"
_DEFAULT_LOG_FILENAME = "ps-service.jsonl"

_lock = threading.Lock()  # guards _default_emitter and _atexit_registered (M13 fix)
_default_emitter: LogEmitter | None = None
_atexit_registered = False


def resolve_default_log_path(*, repo_root: Path | None = None) -> Path:
    """Resolve the default EmitLogEntry sink (AC#5, D3, M12 fix).

    `PS_LOGGING_DIR` overrides the *directory*; the filename is always the
    fixed default `ps-service.jsonl` (M12: PLAN.md defined no default
    filename). Creates the directory eagerly — fail-fast (L1) — because this
    function is only called from `configure()`'s process-default path, a
    legitimate startup boundary; it is NOT used by `LogEmitter`/`emitter.py`
    directly, which must stay silent-fallback-on-failure for an arbitrary
    injected path (AC#6 — see emitter.py `_append_line`). Also enforces that
    the resolved file stays inside the resolved directory (M12's
    "containment guard"): with today's fixed filename constant this can
    never actually trip, but it is the second validation layer L1 mandates
    for file paths, guarding the invariant if the filename is ever made
    configurable.
    """
    base = repo_root if repo_root is not None else _find_repo_root(Path(__file__))
    override = os.environ.get(_LOGGING_DIR_ENV_VAR)
    log_dir = (
        Path(override).expanduser().resolve()
        if override
        else (base / _DEFAULT_LOG_DIRNAME).resolve()
    )
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LoggingConfigurationError(f"cannot create log directory {log_dir}: {exc}") from exc
    log_file = (log_dir / _DEFAULT_LOG_FILENAME).resolve()
    if log_dir not in log_file.parents:
        raise LoggingConfigurationError(
            f"resolved log file {log_file} escapes its directory {log_dir}"
        )
    return log_file


def _find_repo_root(start: Path) -> Path:
    """Walk upward from `start` to the nearest ancestor containing a `.git` directory."""
    for candidate in (start, *start.parents):
        if (candidate / ".git").is_dir():
            return candidate
    raise LoggingConfigurationError(
        f"could not locate a repo root (no .git ancestor) above {start}"
    )


def configure(log_path: Path | None = None, *, fallback: TextSink | None = None) -> LogEmitter:
    """Repoint the process-wide default emitter, stopping the previous one after install.

    `log_path=None` resolves the default via `resolve_default_log_path()`
    (AC#5). Registers an `atexit` drain hook exactly once per process (D9),
    so any entries still queued at normal interpreter exit are flushed.
    **Residual limitation, accepted for this walking skeleton (M7):** a hard
    kill (`SIGKILL`, `os._exit`, a segfault) bypasses `atexit` entirely — no
    daemon thread can survive that — so entries queued at that instant are
    lost. Multi-process/durable delivery is explicitly "under exploration"
    in the architecture doc and out of scope here.
    """
    global _default_emitter, _atexit_registered  # noqa: PLW0603 — module-level default emitter is the documented facade singleton (see module docstring)
    resolved_path = log_path if log_path is not None else resolve_default_log_path()
    emitter = LogEmitter(EmitterConfig(log_path=resolved_path, fallback=fallback))
    with _lock:
        previous = _default_emitter
        _default_emitter = emitter
        if not _atexit_registered:
            atexit.register(_drain_default_emitter_at_exit)
            _atexit_registered = True
    if previous is not None:
        previous.stop()
    return emitter


def _drain_default_emitter_at_exit() -> None:
    """`atexit` hook (D9): best-effort flush of the default emitter's queue."""
    with _lock:
        emitter = _default_emitter
    if emitter is not None:
        with contextlib.suppress(
            LoggingLifecycleError
        ):  # interpreter is tearing down; nothing further to do
            emitter.flush(timeout=5.0)


def reset_for_tests() -> None:
    """Stop and clear the default emitter so each test starts fresh."""
    global _default_emitter
    with _lock:
        emitter, _default_emitter = _default_emitter, None
    if emitter is not None:
        emitter.stop()


def emit_log_entry(
    *,
    component: str,
    action: str,
    entity_id: EntityId = None,
    outcome: str | None = None,
    duration_ms: float | None = None,
    run_id: str | None = None,
    extra: Mapping[str, object] | None = None,
    emitter: LogEmitter | None = None,
) -> None:
    """Build a `LogEntry` and enqueue it on `emitter` (or the process default).

    Bakes the *current* bound run_id (`run_context.current_run_id()`) if
    `run_id` is not explicitly given — AC#1's propagation mechanism: this
    read happens here, on the caller's thread, at enqueue time, never inside
    the writer thread (see emitter.py's module docstring).

    Non-blocking (AC#4): builds the entry and calls `LogEmitter.emit`, which
    only enqueues — no file I/O on this thread. Never raises to the caller
    for a write/serialize failure (AC#6): that is handled entirely inside
    the writer thread. Raises `LoggingLifecycleError` only for the caller's
    own wiring misuse — no `emitter` given and no default `configure()`d —
    which is a programming error to surface immediately, not a runtime write
    failure to swallow.
    """
    if emitter is not None:
        target: LogEmitter | None = emitter
    else:
        with _lock:
            target = _default_emitter
    if target is None:
        raise LoggingLifecycleError(
            "emit_log_entry() called with no emitter and no default configured; "
            "call configure() at process start or pass emitter= explicitly"
        )
    entry = LogEntry(
        component=component,
        action=action,
        run_id=run_id if run_id is not None else current_run_id(),
        entity_id=entity_id,
        outcome=outcome,
        duration_ms=duration_ms,
        extra=tuple(extra.items()) if extra else (),
    )
    target.emit(entry)
