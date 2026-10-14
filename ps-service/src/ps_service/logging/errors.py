"""Exception types raised by the setup and lifecycle API of `ps_service.logging`.

Neither type is raised for a write or serialization failure: those are handled entirely
inside the emitter's writer thread, which falls back to stderr, so logging never breaks the
caller's work. These types signal genuine misuse or an unusable environment, where failing
fast is appropriate.
"""


class LoggingConfigurationError(Exception):
    """The log sink's location could not be resolved or prepared.

    Raised by `resolve_default_log_path()` and `configure()` when the repository root cannot
    be located, when the target log directory cannot be created, or when the resolved log
    file path would escape its directory. The last check is a second validation layer on a
    security-relevant file path.
    """


class LoggingLifecycleError(Exception):
    """The caller misused a `LogEmitter`'s lifecycle or the logging facade's wiring.

    Raised by `LogEmitter.flush()` when the writer thread does not acknowledge within the
    timeout (including a flush after `stop()`, since the stopped writer never acknowledges),
    and by `emit_log_entry()` when no `emitter` was given and no process default emitter has
    been configured.
    """
