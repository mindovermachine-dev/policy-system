"""Domain-specific exception types for ps_service.logging.

L1/L2: no generic `Exception` is raised to a caller of this component's
setup/lifecycle API.

Neither type is ever raised by the hot path (`emit_log_entry`,
`bind_run_context`, `LogEmitter.emit`) — AC#6 requires those to never raise;
a write/serialize failure is handled entirely inside the writer thread with
a stderr fallback (see emitter.py). These two types exist for genuine caller
misuse of the surrounding setup/lifecycle API, which IS an appropriate place
to fail fast (L1 "Fail Fast at Boundaries").
"""


class LoggingConfigurationError(Exception):
    """The log sink's location could not be resolved or prepared.

    Raised by `facade.resolve_default_log_path()` / `facade.configure()` when
    the target log directory cannot be created, or when a resolved log file
    path would escape its configured directory — defense-in-depth for a
    security-relevant file path, per L1's explicit file-path carve-out
    ("security-critical sinks... get a second validation layer at
    point-of-use").
    """


class LoggingLifecycleError(Exception):
    """The caller misused a `LogEmitter`'s lifecycle or the facade's wiring.

    Raised by `LogEmitter.flush()` on a bounded-wait timeout (G3) — including
    the case of flushing after `stop()`, since the writer thread will never
    pop the sentinel — and by `emit_log_entry()` when no `emitter` was given
    and no process default has been `configure()`d.
    """
