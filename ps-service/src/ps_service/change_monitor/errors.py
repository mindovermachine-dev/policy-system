"""Domain-specific exception types for `ps_service.change_monitor`.

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1 Error Handling, L2 Error Handling).
Mirrors the shape of `ps_service.ingestion.errors` /
`ps_service.company_merge.errors`, but with a shared `ChangeMonitorError`
base so a caller can catch every change-monitor failure with one `except`
(PLAN_REVIEWED.md §2 "Error types").
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

    Lets a caller catch any change-monitor failure with a single `except`
    while still allowing precise handling of each concrete subclass.
    """


class ChangeMonitorConfigurationError(ChangeMonitorError):
    """The FalkorDB connection could not be established or validated.

    The resolved `ServiceConfig` gave an unreachable host/port, or
    connectivity could not be confirmed. Raised by
    `falkordb_client.check_connectivity`.
    """


class CellarConsolidationQueryError(ChangeMonitorError):
    """The CELLAR SPARQL consolidated-version query could not be completed.

    Transport failure, a non-200 response, a body that is not the expected
    SPARQL-results JSON shape, or a base-act CELEX that is malformed or
    outside the legislation sector. Raised by
    `cellar_consolidated.fetch_consolidated_versions`.
    """


class SuccessionPersistenceError(ChangeMonitorError):
    """A FalkorDB write in `succession.py` could not be completed safely.

    Wraps the underlying `redis.exceptions.RedisError` and marks FalkorDB
    unhealthy, mirroring `ingestion.graph_writer._execute_query`.
    """


class NationalTranspositionNotSupportedError(ChangeMonitorError):
    """`trigger_reingestion` was invoked for a `national_transposition` instrument.

    The change monitor supports `regulation` and `directive` framework
    instruments only. National transposition modeling (#41) and its
    succession/versioning story (#46) are deferred; this guard raises before
    any graph write so the prior instrument is left untouched (AC-010).
    """

    def __init__(self, message: str = _NATIONAL_TRANSPOSITION_NOT_SUPPORTED_MESSAGE) -> None:
        """Initialise with a default message that names issues #41 and #46."""
        super().__init__(message)


class ChangeMonitorStateError(ChangeMonitorError):
    """The graph is in a state `trigger_reingestion` cannot proceed from.

    No active prior `RegulatoryInstrument` for the given short name, or more
    than one — a genuinely inconsistent graph rather than a crash artifact,
    since the prior lookup already excludes the new node and any node
    already superseded into it. Raised by `succession.find_prior_instrument`.
    """
