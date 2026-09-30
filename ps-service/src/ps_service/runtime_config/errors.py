"""Domain-specific exception types for `ps_service.runtime_config` (issue #130).

One exception type per distinct failure boundary this component owns, never a generic
`Exception`/`ValueError` (L1/L2 Error Handling). Every type derives from
`RuntimeConfigError` so a read path that must fail closed on *any* config problem can
catch one base type.

The two caller-input errors (`RuntimeConfigUnknownKeyError`, `RuntimeConfigInvalidValueError`)
are raised before any connection is opened or row written. The two store-boundary errors
(`RuntimeConfigUnavailableError`, `RuntimeConfigPersistenceError`) carry fixed messages that
never contain host, port, driver text or the value being written.
"""

from __future__ import annotations


class RuntimeConfigError(Exception):
    """Base type of every `ps_service.runtime_config` error."""


class RuntimeConfigUnknownKeyError(RuntimeConfigError):
    """The named key is not in the registry (AC-BI-005); raised before any connection or write."""


class RuntimeConfigInvalidValueError(RuntimeConfigError):
    """A value failed its key's type check or validator (AC-BI-005).

    Raised before any write on `set`, and on `get` when a stored row no longer validates
    (a hand-edited row never reaches a caller). The message is the validator's own reason,
    or a fixed sentence for a type mismatch -- never the offending value itself.
    """


UNAVAILABLE_MESSAGE = "The runtime configuration store is temporarily unavailable."
PERSISTENCE_MESSAGE = "The runtime configuration write failed and was not applied."


class RuntimeConfigUnavailableError(RuntimeConfigError):
    """The PS state Postgres could not be reached or read (AC-BI-010).

    Fixed, detail-free message.
    """

    def __init__(self, message: str = UNAVAILABLE_MESSAGE) -> None:
        """Default to the one fixed message; never build it from driver or connection text."""
        super().__init__(message)


class RuntimeConfigPersistenceError(RuntimeConfigError):
    """A `set`/`reset` write or its `audit_events` insert failed (AC-BI-011).

    Raised after the transaction was rolled back, so neither the state change nor the audit
    row was applied. Fixed, detail-free message; the driver error is chained via `from`.
    """

    def __init__(self, message: str = PERSISTENCE_MESSAGE) -> None:
        """Default to the one fixed message; never build it from driver text."""
        super().__init__(message)
