"""Public API for ps_service.logging: BindRunContext and EmitLogEntry."""

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
