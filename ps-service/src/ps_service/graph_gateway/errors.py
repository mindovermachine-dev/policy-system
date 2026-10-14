"""Domain-specific exception types of the Graph Write Gateway and its mutation log store.

One type per failure boundary, never a generic `Exception` (L1/L2 Error Handling). Messages are
fixed strings that carry no host, port, user, SQL or driver text; the driver error is chained as
`__cause__`.
"""

from __future__ import annotations

from ps_service.persistence import StatePostgresConnectionError

GRAPH_LOG_UNAVAILABLE_MESSAGE = "The graph mutation log is temporarily unavailable."
"""The only text a `GraphLogUnavailableError` ever carries."""


class GraphLogUnavailableError(StatePostgresConnectionError):
    """The PS state Postgres holding the graph log could not be reached or is unconfigured.

    A `StatePostgresConnectionError`, so every handler of the existing persistence error also
    catches it. The message is the fixed `GRAPH_LOG_UNAVAILABLE_MESSAGE`.
    """

    def __init__(self) -> None:
        """Create the error with `GRAPH_LOG_UNAVAILABLE_MESSAGE` and no other text."""
        super().__init__(GRAPH_LOG_UNAVAILABLE_MESSAGE)


GRAPH_UNAVAILABLE_MESSAGE = "The graph store is temporarily unavailable."
"""The only text a `GraphUnavailableError` ever carries."""


class GraphUnavailableError(Exception):
    """FalkorDB stayed unreachable through the gateway's bounded retries.

    A fixed message with no host, port or driver text (the redis error is chained as
    `__cause__`). Deliberately not a `StatePostgresConnectionError`: it is about the graph store,
    not the PS state Postgres. Raised only where nothing was committed, or from a
    catch-up that could not finish; after a commit the caller sees `committed_apply_pending`.
    """

    def __init__(self) -> None:
        """Create the error with `GRAPH_UNAVAILABLE_MESSAGE` and no other text."""
        super().__init__(GRAPH_UNAVAILABLE_MESSAGE)


class GraphApplyError(Exception):
    """FalkorDB refused a query the gateway issued for a reason retrying cannot fix.

    Carries the graph and, when the failure happened applying a logged entry, the position of the
    first entry that is not applied. Never any query, payload or driver text; the driver error is
    chained as `__cause__`.
    """

    def __init__(self, graph: str, position: int | None = None) -> None:
        """Record the graph and the first unapplied position.

        Args:
            graph: Name of the graph the write targeted.
            position: Log position of the first entry not applied; None when the failure
                happened before anything was logged.
        """
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
        """Record the blocked graph.

        Args:
            graph: Name of the graph that holds the unappliable entry.
        """
        super().__init__(graph)
        self.args = (f"graph {graph} is blocked by a logged entry that could not be applied",)


class GraphReplayGatedError(GraphApplyError):
    """A write was refused because the graph's replay has not completed or has failed.

    Deliberately a `GraphApplyError` but not a `GraphApplyBlockedError`: callers that treat a
    refused graph as gated (`recover()`, the reconciler) already handle it, and the message does
    not claim a logged entry could not be applied. `position` is the log position the failed
    replay stopped at, or None while the replay is still running.
    """

    def __init__(self, graph: str, position: int | None = None, *, failed: bool = False) -> None:
        """Record the graph and why it is gated.

        Args:
            graph: Name of the gated graph.
            position: Log position the replay stopped at; None while it is still running.
            failed: True when the replay failed at `position`, False when it has not completed.
        """
        super().__init__(graph, position)
        reason = (
            f"replay failed at log position {position}" if failed else "replay has not completed"
        )
        self.args = (f"graph {graph} is gated: {reason}",)


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
        """Record the graph and the two log positions.

        Args:
            graph: Name of the graph the group targeted.
            expected_position: Log position the caller prepared the group against.
            actual_position: Log position the log is at.
        """
        super().__init__(
            f"graph {graph} is at log position {actual_position}, "
            f"the group expected {expected_position}"
        )
        self.graph = graph
        self.expected_position = expected_position
        self.actual_position = actual_position


class SwapNotConfiguredError(GraphWriteRejectedError):
    """A group asked to be applied by swap, but this gateway has no way to copy and swap graphs.

    Refused before anything is logged: the caller can submit the group to be applied in place, or
    the service can be given a gateway that was built with a key port.
    """


class SwapConflictError(Exception):
    """A watched live graph changed while a swap was being prepared, so the swap was not made.

    Raised by a key port when the atomic rename detects a writer that did not go through the
    gateway. The gateway starts the swap again from a fresh copy; the message names nothing.
    """


class UnexpectedGraphReplyError(Exception):
    """A read answered by the graph does not have the shape the gateway's query promises.

    The message names nothing the graph returned.
    """


class GraphDigestError(Exception):
    """A graph cannot be reduced to a canonical digest.

    Raised when a read answers an unexpected shape, or a node, edge or property holds something
    the gateway cannot have written. The message is fixed and names nothing the graph returned.
    """


class GraphReplayError(Exception):
    """A replay of one graph from the log failed.

    Carries the graph and the log position the failure is about. Messages name the two and
    nothing else: never content, never a digest.
    """

    def __init__(self, graph: str, position: int, reason: str) -> None:
        """Record the graph and position.

        Args:
            graph: Name of the graph being replayed.
            position: Log position the failure is about.
            reason: A fixed phrase that names neither content nor a digest.
        """
        super().__init__(f"replay of graph {graph} failed at log position {position}: {reason}")
        self.graph = graph
        self.position = position


class GraphDigestMismatchError(GraphReplayError):
    """The rebuilt graph's digest differs from the checkpoint the log holds for its position.

    The two digests are attributes (`expected`, `actual`), not part of the message.
    """

    def __init__(self, graph: str, position: int, *, expected: str, actual: str) -> None:
        """Record the graph, the checkpoint position and both digests.

        Args:
            graph: Name of the replayed graph.
            position: Log position of the checkpoint.
            expected: Digest the checkpoint holds.
            actual: Digest of the rebuilt graph.
        """
        super().__init__(graph, position, "the rebuilt graph does not match its checkpoint")
        self.expected = expected
        self.actual = actual


class GraphLogGapError(GraphReplayError):
    """The log skips a position the replay expected next (the sequence is not contiguous).

    The database forbids this; the check is defence in depth. `position` is the missing one.
    """

    def __init__(self, graph: str, position: int) -> None:
        """Record the graph and the missing position.

        Args:
            graph: Name of the replayed graph.
            position: The log position that has no entry.
        """
        super().__init__(graph, position, "the log has no entry at this position")


class GraphLogCorruptEntryError(GraphReplayError):
    """A logged entry does not decode to a graph mutation. The decode error is the `__cause__`."""

    def __init__(self, graph: str, position: int) -> None:
        """Record the graph and the undecodable entry.

        Args:
            graph: Name of the replayed graph.
            position: Log position of the entry that does not decode.
        """
        super().__init__(graph, position, "the log entry does not decode")


class GraphReplayUnverifiableError(GraphReplayError):
    """A resumed replay is past its checkpoint and never compared the graph with it.

    The progress record says the replay went on beyond the checkpoint without a recorded
    verification, so the partial graph cannot be vouched for and nothing may be rebuilt on top of
    it. `position` is the checkpoint's. The graph is left as it is.
    """

    def __init__(self, graph: str, position: int) -> None:
        """Record the graph and the unverified checkpoint.

        Args:
            graph: Name of the replayed graph.
            position: Log position of the checkpoint the replay never compared against.
        """
        super().__init__(graph, position, "the progress passed this checkpoint unverified")


class GraphReplayStoppedError(Exception):
    """A replay was asked to stop (service shutdown) and stopped between two pages.

    Not a failure of the graph: the progress record is intact, the graph stays closed to writes
    as incomplete, and a later replay resumes after `position`, the last position applied.
    """

    def __init__(self, graph: str, position: int) -> None:
        """Record the graph and where the replay stopped.

        Args:
            graph: Name of the replayed graph.
            position: Last log position applied before the stop.
        """
        super().__init__(f"replay of graph {graph} stopped after log position {position}")
        self.graph = graph
        self.position = position


class StagedSubmissionClosedError(Exception):
    """A staged in-transaction submission was used again after it had been completed or aborted."""


class StagedGroupNotCommittedError(Exception):
    """`complete()` found the staged group missing from the log: the caller has not committed.

    The submission is closed and the graph lock released, as after any `complete()`.
    """
