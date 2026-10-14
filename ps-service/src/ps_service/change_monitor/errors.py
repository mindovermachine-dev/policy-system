"""Domain-specific exception types for `ps_service.change_monitor`.

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError`. Every type derives from `ChangeMonitorError`
so a caller can catch every change-monitor failure with one `except` while
still handling each concrete failure precisely.
"""

from __future__ import annotations

_NATIONAL_TRANSPOSITION_NOT_SUPPORTED_MESSAGE = (
    "Re-ingestion of a national_transposition instrument is not supported by the "
    "regulatory change monitor. National transposition modeling (#41) and its "
    "succession/versioning story (#46) are tracked separately; resolve those "
    "before driving national transposition succession through trigger_reingestion."
)


class ChangeMonitorError(Exception):
    """Base class for every error raised by `ps_service.change_monitor`.

    Never raised directly. Catch it to handle any change-monitor failure with a
    single `except`; catch a subclass to handle one failure boundary.
    """


class ChangeMonitorConfigurationError(ChangeMonitorError):
    """The FalkorDB connection could not be established or validated.

    Raised by `falkordb_client.check_connectivity` when the resolved
    `ServiceConfig` names an unreachable host/port or connectivity cannot be
    confirmed. The caller should fix the service configuration or database
    availability and retry.
    """


class CellarConsolidationQueryError(ChangeMonitorError):
    """The CELLAR SPARQL consolidated-version query could not be completed.

    Transport failure, a non-200 response, a body that is not the expected
    SPARQL-results JSON shape, or a base-act CELEX that is malformed or
    outside the legislation sector. Raised by
    `cellar_consolidated.fetch_consolidated_versions`. The caller may retry a
    transport failure; a malformed CELEX is a caller error and will not succeed.
    """


class SuccessionPersistenceError(ChangeMonitorError):
    """A FalkorDB write in `succession.py` could not be completed safely.

    Wraps the underlying `redis.exceptions.RedisError` and marks FalkorDB
    unhealthy. The succession may be partially recorded; the caller should
    re-run `trigger_reingestion`, which repairs an incomplete succession.
    """


class NationalTranspositionNotSupportedError(ChangeMonitorError):
    """`trigger_reingestion` was invoked for a `national_transposition` instrument.

    The change monitor supports `regulation` and `directive` framework
    instruments only; national transposition succession and versioning is not
    modelled. The guard raises before any graph write, so the prior instrument
    is left untouched. The caller should not retry.

    Args:
        message: Text of the error; defaults to a message explaining that
            national transposition re-ingestion is unsupported.
    """

    def __init__(self, message: str = _NATIONAL_TRANSPOSITION_NOT_SUPPORTED_MESSAGE) -> None:
        """Store `message` as the error text."""
        super().__init__(message)


class ChangeMonitorStateError(ChangeMonitorError):
    """The graph is in a state `trigger_reingestion` cannot proceed from.

    No active prior `RegulatoryInstrument` for the given short name, or more
    than one — a genuinely inconsistent graph rather than a crash artifact,
    since the prior lookup already excludes the new node and any node
    already superseded into it. Raised by `succession.find_prior_instrument`.
    The caller should inspect the graph for the short name before retrying.
    """
