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


GRAPH_UNAVAILABLE_MESSAGE = "The graph store is temporarily unavailable."
"""The only text a `GraphUnavailableError` ever carries (AC-BI-009)."""


class GraphUnavailableError(Exception):
    """FalkorDB stayed unreachable through the gateway's bounded retries.

    A fixed message with no host, port or driver text (the redis error is chained as
    `__cause__`). Deliberately not a `StatePostgresConnectionError`: it is about the graph store,
    not the PS state Postgres (CHANGES D6). Raised only where nothing was committed, or from a
    catch-up that could not finish; after a commit the caller sees `committed_apply_pending`.
    """

    def __init__(self) -> None:
        """Carry the fixed, sanitized message."""
        super().__init__(GRAPH_UNAVAILABLE_MESSAGE)


class GraphApplyError(Exception):
    """FalkorDB refused a query the gateway issued for a reason retrying cannot fix.

    Carries the graph and, when the failure happened applying a logged entry, the position of the
    first entry that is not applied. Never any query, payload or driver text; the driver error is
    chained as `__cause__`.
    """

    def __init__(self, graph: str, position: int | None = None) -> None:
        """Record the graph and the first unapplied position (None before anything was logged)."""
        where = "" if position is None else f" at log position {position}"
        super().__init__(f"graph {graph} rejected a gateway write{where}")
        self.graph = graph
        self.position = position


class GraphApplyBlockedError(GraphApplyError):
    """A write was refused because the graph holds a logged entry FalkorDB cannot apply.

    The graph stays blocked, and no new group is logged for it, until the process restarts or an
    explicit `catch_up` applies the entry.
    """

    def __init__(self, graph: str) -> None:
        """Record the blocked graph."""
        super().__init__(graph)
        self.args = (f"graph {graph} is blocked by a logged entry that could not be applied",)


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


class GraphLogEntryDecodeError(Exception):
    """A recorded log entry does not decode to a known graph mutation.

    The message names the entry's position only, never its content.
    """


class GraphWriteRejectedError(Exception):
    """A group was refused before anything was logged or applied; the caller can fix and resubmit.

    Messages never carry caller content: they point at the primitive by its index in the group.
    """


class UnlistedNameError(GraphWriteRejectedError):
    """A group names a node label or relationship type outside the gateway's allow-list."""


class MissingTargetError(GraphWriteRejectedError):
    """An edge names an endpoint node that neither exists in the graph nor is created earlier."""


class StaleGraphStateError(GraphWriteRejectedError):
    """The log has moved past the position the caller's group was prepared against.

    Carries the graph and the two positions, never any group content.
    """

    def __init__(self, graph: str, expected_position: int, actual_position: int) -> None:
        """Record the graph, the position the caller expected and the position the log is at."""
        super().__init__(
            f"graph {graph} is at log position {actual_position}, "
            f"the group expected {expected_position}"
        )
        self.graph = graph
        self.expected_position = expected_position
        self.actual_position = actual_position


class UnexpectedGraphReplyError(Exception):
    """A read answered by the graph does not have the shape the gateway's query promises.

    The message names nothing the graph returned.
    """


class StagedSubmissionClosedError(Exception):
    """A staged in-transaction submission was completed after it was completed or aborted."""


class StagedGroupNotCommittedError(Exception):
    """`complete()` found the staged group missing from the log: the caller has not committed.

    The submission is closed and the graph lock released, as after any `complete()`.
    """
