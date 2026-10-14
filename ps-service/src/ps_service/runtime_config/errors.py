"""Domain-specific exception types for `ps_service.runtime_config`.

One exception type per distinct failure boundary this component owns, never a generic
`Exception`/`ValueError`. Every type derives from
`RuntimeConfigError` so a read path that must fail closed on *any* config problem can
catch one base type.

The two caller-input errors (`RuntimeConfigUnknownKeyError`, `RuntimeConfigInvalidValueError`)
are raised before any connection is opened or row written. The two store-boundary errors
(`RuntimeConfigUnavailableError`, `RuntimeConfigPersistenceError`) carry fixed messages that
never contain host, port, driver text or the value being written.
"""

from __future__ import annotations


class RuntimeConfigError(Exception):
    """Base type of every `ps_service.runtime_config` error.

    Catch this type where a read path must fail closed on any runtime-configuration problem.
    """


class RuntimeConfigUnknownKeyError(RuntimeConfigError):
    """The named key is not in the registry.

    Raised before any database connection is opened or row written. The caller supplied a key
    name that no component registered; correct the name rather than retry.
    """


class RuntimeConfigInvalidValueError(RuntimeConfigError):
    """A value failed its key's type check or validator.

    Raised before any write on `set`, and on `get` when a stored row fails validation
    (a hand-edited row never reaches a caller). The message is the validator's own reason,
    or a fixed sentence for a type mismatch -- never the offending value itself. For a
    supplied value the caller should send a corrected one; for a stored row the key must be
    reset or overwritten with a valid value.
    """


UNAVAILABLE_MESSAGE = "The runtime configuration store is temporarily unavailable."
PERSISTENCE_MESSAGE = "The runtime configuration write failed and was not applied."


class RuntimeConfigUnavailableError(RuntimeConfigError):
    """The PS state Postgres could not be reached or read.

    Fixed, detail-free message. The caller fails closed and may retry later; the driver error
    is chained via `from`.
    """

    def __init__(self, message: str = UNAVAILABLE_MESSAGE) -> None:
        """Build the error with a fixed message.

        Args:
            message: Text of the error; defaults to the fixed `UNAVAILABLE_MESSAGE`. Callers
                must not build it from host, port or driver text.
        """
        super().__init__(message)


class RuntimeConfigPersistenceError(RuntimeConfigError):
    """A `set`/`reset` write or its `audit_events` insert failed.

    Raised after the transaction was rolled back, so neither the state change nor the audit
    row was applied. Fixed, detail-free message; the driver error is chained via `from`.
    """

    def __init__(self, message: str = PERSISTENCE_MESSAGE) -> None:
        """Build the error with a fixed message.

        Args:
            message: Text of the error; defaults to the fixed `PERSISTENCE_MESSAGE`. Callers
                must not build it from driver text.
        """
        super().__init__(message)
