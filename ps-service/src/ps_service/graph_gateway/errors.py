"""Domain-specific exception types for the graph mutation log store (issue #205).

One type per failure boundary, never a generic `Exception` (L1/L2 Error Handling). Messages are
fixed strings that carry no host, port, user, SQL or driver text; the driver error is chained as
`__cause__`.
"""

from __future__ import annotations

from ps_service.persistence import StatePostgresConnectionError

GRAPH_LOG_UNAVAILABLE_MESSAGE = "The graph mutation log is temporarily unavailable."
"""The only text a `GraphLogUnavailableError` ever carries (AC-BI-009)."""


class GraphLogUnavailableError(StatePostgresConnectionError):
    """The PS state Postgres holding the graph log could not be reached or is unconfigured.

    A `StatePostgresConnectionError`, so every handler of the existing persistence error also
    catches it (AC-BI-009). The message is the fixed `GRAPH_LOG_UNAVAILABLE_MESSAGE`.
    """

    def __init__(self) -> None:
        """Carry the fixed, sanitized message."""
        super().__init__(GRAPH_LOG_UNAVAILABLE_MESSAGE)


class GraphLogPersistenceError(Exception):
    """A graph log write or its transaction preconditions failed after the store was reached.

    Raised when an insert, the sequence lock or the commit fails, or when the caller's
    transaction cannot give the append its guarantees (not READ COMMITTED, not in a
    transaction). Nothing of the failed group remains. The driver error, if any, is chained.
    """


class GraphLogPayloadError(GraphLogPersistenceError):
    """An entry's content or embedding cannot be turned into a storable payload.

    Raised before any SQL runs (for example content JSON cannot carry), so nothing was written.
    A `GraphLogPersistenceError`, so handlers of failed appends also catch it.
    """
