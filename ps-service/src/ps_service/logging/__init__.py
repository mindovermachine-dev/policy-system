"""Public API for `ps_service.logging`.

Purpose: structured, non-blocking JSON-lines logging with run correlation. `bind_run_context`
and `current_run_id` correlate log entries across a call chain; `emit_log_entry` records an
entry without blocking the caller; `configure` installs the process-wide emitter. The
exports also include the emitter, its configuration and sink protocols, the `LogEntry`
record and the two error types.
"""

from ps_service.logging.emitter import (
    EmitterConfig,
    LogEmitter,
    TextSink,
    WriterFactory,
    default_writer_factory,
)
from ps_service.logging.errors import LoggingConfigurationError, LoggingLifecycleError
from ps_service.logging.facade import (
    configure,
    emit_log_entry,
    reset_for_tests,
    resolve_default_log_path,
)
from ps_service.logging.models import EntityId, LogEntry
from ps_service.logging.run_context import bind_run_context, current_run_id

__all__ = [
    "EmitterConfig",
    "EntityId",
    "LogEmitter",
    "LogEntry",
    "LoggingConfigurationError",
    "LoggingLifecycleError",
    "TextSink",
    "WriterFactory",
    "bind_run_context",
    "configure",
    "current_run_id",
    "default_writer_factory",
    "emit_log_entry",
    "reset_for_tests",
    "resolve_default_log_path",
]
