"""Process-wide entry point for emitting log entries.

Purpose: own the one piece of process-wide state in the logging component, the default
`LogEmitter` that `configure()` installs, and expose `emit_log_entry` to write through it.
A process-wide default is acceptable infrastructure, and every test can substitute its own
emitter through `emit_log_entry`'s `emitter` parameter. Run-id binding needs no process state
and lives in `ps_service.logging.run_context`.

Thread safety: the default emitter reference is guarded by a lock.
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

_LOGGING_DIR_ENV_VAR = "PS_LOGGING_DIR"
_DEFAULT_LOG_DIRNAME = "logs"
_DEFAULT_LOG_FILENAME = "ps-service.jsonl"

_lock = threading.Lock()  # guards _default_emitter and _atexit_registered
_default_emitter: LogEmitter | None = None
_atexit_registered = False


def resolve_default_log_path(*, repo_root: Path | None = None) -> Path:
    """Resolve the default log file path and create its directory.

    The directory is `PS_LOGGING_DIR` when that variable is set and non-empty (with `~`
    expanded), otherwise a `logs` directory under the repository root. The file name is
    always `ps-service.jsonl`. The directory is created eagerly so a misconfiguration fails
    at startup; `LogEmitter` itself never calls this function and instead falls back silently
    when an injected path cannot be written. The function also checks that the resolved file
    stays inside the resolved directory. With the fixed file name this cannot fail today; it
    guards the invariant if the name is ever made configurable.

    Args:
        repo_root: Directory to treat as the repository root for the default `logs`
            location. None searches upward from this module for the nearest ancestor holding
            a `.git` directory. Ignored when `PS_LOGGING_DIR` is set.

    Returns:
        The absolute, resolved path of the log file.

    Raises:
        LoggingConfigurationError: No repository root could be found (only when `repo_root`
            is None), the log directory cannot be created, or the resolved file path escapes
            the directory.

    Side Effects:
        Reads the `PS_LOGGING_DIR` environment variable and creates the log directory
        (including parents) if absent.
    """
    override = os.environ.get(_LOGGING_DIR_ENV_VAR)
    if override:
        log_dir = Path(override).expanduser().resolve()
    else:
        base = repo_root if repo_root is not None else _find_repo_root(Path(__file__))
        log_dir = (base / _DEFAULT_LOG_DIRNAME).resolve()
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
    """Find the nearest ancestor of `start` (or `start` itself) containing a `.git` directory.

    Args:
        start: Path to begin the upward search from.

    Returns:
        The first directory on the way up that contains a `.git` directory.

    Raises:
        LoggingConfigurationError: No such ancestor exists.
    """
    for candidate in (start, *start.parents):
        if (candidate / ".git").is_dir():
            return candidate
    raise LoggingConfigurationError(
        f"could not locate a repo root (no .git ancestor) above {start}"
    )


def configure(
    log_path: Path | None = None,
    *,
    fallback: TextSink | None = None,
    mirror_failures_to_stderr: bool = False,
) -> LogEmitter:
    """Install a new process-wide default emitter and stop the previous one.

    The new emitter is installed before the previous one is stopped, so entries emitted
    meanwhile go to the new one. An `atexit` hook that flushes the default emitter is
    registered once per process, so entries still queued at normal interpreter exit are
    written. Known limitation: a hard kill (`SIGKILL`, `os._exit`, a crash) bypasses `atexit`
    and loses entries queued at that instant; durable or multi-process delivery is not
    provided.

    Args:
        log_path: File to append JSON lines to. None resolves the default through
            `resolve_default_log_path()`.
        fallback: Sink that receives a line the emitter cannot write to the log file. None
            resolves `sys.stderr` at failure time.
        mirror_failures_to_stderr: When True, every entry whose outcome is `"failed"` is also
            written to the fallback sink (stderr by default), so a failure cause is visible
            in container logs even though the log file is not.

    Returns:
        The newly installed `LogEmitter`.

    Raises:
        LoggingConfigurationError: `log_path` is None and the default path cannot be resolved
            or its directory cannot be created.

    Side Effects:
        Starts a daemon writer thread for the new emitter, replaces the process default,
        registers the `atexit` hook on first call, and stops the previous emitter (draining
        its queue and waiting up to its stop timeout). May create the default log directory.
    """
    global _default_emitter, _atexit_registered  # noqa: PLW0603 — module-level default emitter is the documented facade singleton (see module docstring)
    resolved_path = log_path if log_path is not None else resolve_default_log_path()
    emitter = LogEmitter(
        EmitterConfig(
            log_path=resolved_path,
            fallback=fallback,
            mirror_failures_to_stderr=mirror_failures_to_stderr,
        )
    )
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
    """Flush the default emitter's queue at interpreter exit, on a best-effort basis.

    Does nothing when no default emitter is installed. A flush timeout is swallowed because
    the interpreter is shutting down and nothing further can be done.

    Side Effects:
        Blocks up to five seconds while the writer thread drains queued entries.
    """
    with _lock:
        emitter = _default_emitter
    if emitter is not None:
        with contextlib.suppress(
            LoggingLifecycleError
        ):  # interpreter is tearing down; nothing further to do
            emitter.flush(timeout=5.0)


def reset_for_tests() -> None:
    """Stop and clear the default emitter and re-arm the `atexit` registration guard.

    Intended for test isolation. The guard must be re-armed together with the emitter:
    leaving it set would make the next `configure()` skip registering the `atexit` hook, so
    the once-per-process registration that `configure()` documents could not be observed from
    a clean start.

    Returns:
        None.

    Side Effects:
        Clears the process default emitter and stops the previous one (draining its queue and
        waiting up to its stop timeout). A later `configure()` registers the `atexit` hook
        again.
    """
    global _default_emitter, _atexit_registered  # noqa: PLW0603 — mirrors configure()'s module-level singleton reset (see module docstring)
    with _lock:
        emitter, _default_emitter = _default_emitter, None
        _atexit_registered = False
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
    """Build a log entry and enqueue it for asynchronous writing.

    The call does not block on I/O: it only enqueues the entry, and a write or serialization
    failure is handled inside the emitter's writer thread, never raised here. When `run_id`
    is not given, the run id bound on the calling thread at this moment is recorded; it is
    read here, not later in the writer thread, so it reflects the caller's context.

    Args:
        component: Name of the emitting component; required.
        action: Name of the operation being logged; required.
        entity_id: One entity id, a tuple of entity ids, or None.
        outcome: Result label such as `"success"` or `"failed"`, or None. With an emitter
            configured to mirror failures, `"failed"` is also written to stderr.
        duration_ms: Elapsed time in milliseconds, or None.
        run_id: Explicit correlation id. None records the currently bound run id, if any.
        extra: Additional key/value pairs to record. Keys that collide with a convention field
            name are dropped when written. Never include secrets or personal data. None or an
            empty mapping records none.
        emitter: Emitter to enqueue on. None uses the process default installed by
            `configure()`.

    Returns:
        None.

    Raises:
        LoggingLifecycleError: `emitter` is None and no default emitter has been configured.
            This is a wiring error surfaced immediately, not a write failure.

    Side Effects:
        Enqueues one entry on the emitter; the file write happens later on the writer thread.
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
