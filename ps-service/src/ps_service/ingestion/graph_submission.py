"""The ingestion side of the Graph Write Gateway seam.

Ingestion never holds a write handle to FalkorDB: it builds a `MutationGroup` and submits it
through a `GroupSubmitter`. `GraphWriteGateway` satisfies that Protocol structurally, so ingestion
depends on the narrow call it needs and a test can substitute the gateway rig.

`submit_or_raise` is the one place gateway failures become the error type of the module that
submitted the group, with the message prefix `FalkorDB write failed`, which the API's failure
classification relies on. The gateway's messages are fixed and sanitized (no host, port, query or
content), so they are safe to carry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ps_service.graph_gateway.errors import (
    GraphApplyError,
    GraphLogPersistenceError,
    GraphUnavailableError,
    GraphWriteRejectedError,
    UnexpectedGraphReplyError,
)
from ps_service.persistence import StatePostgresConnectionError

if TYPE_CHECKING:
    from ps_service.graph_gateway.edge_identity_backfill import EdgeIdentityBackfillOutcome
    from ps_service.graph_gateway.graph_reader import PersistedElements
    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup

_WRITE_FAILED_PREFIX = "FalkorDB write failed: "
_COMMITTED_NOT_APPLIED_MESSAGE = (
    "the group was committed to the graph log but could not be applied to the graph yet"
)

_GATEWAY_FAILURES = (
    GraphWriteRejectedError,
    GraphApplyError,
    GraphUnavailableError,
    GraphLogPersistenceError,
    UnexpectedGraphReplyError,
    StatePostgresConnectionError,
)
"""Every failure a submit raises on purpose; anything else is a bug and propagates untouched."""


@runtime_checkable
class GroupSubmitter(Protocol):
    """What ingestion needs of the Graph Write Gateway: submit a group and read its graph state.

    `GraphWriteGateway` satisfies this structurally; tests can substitute a fake.
    """

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        """Log `group` to the graph log and apply it to its graph.

        Args:
            group: The mutation group to submit.

        Returns:
            The group's outcome, including whether it was applied or is committed but pending.
        """
        ...

    def is_caught_up(self, graph: str) -> bool:
        """Report whether every committed log entry of `graph` is applied to it.

        Args:
            graph: Name of the graph.

        Returns:
            True only if no committed entry is waiting to be applied.
        """
        ...

    def stamp_legacy_edge_identity(
        self, graph: str, audit_event_id: str | None
    ) -> EdgeIdentityBackfillOutcome:
        """Give the identity-less edges of `graph` their identity (a no-op read if none).

        Args:
            graph: Name of the graph.
            audit_event_id: Audit event the stamp group is logged under, or None.

        Returns:
            The backfill outcome.
        """
        ...

    def read_persisted_elements(self, graph: str) -> PersistedElements:
        """Read the node keys, edge keys and log position of `graph` (read-only).

        Args:
            graph: Name of the graph.

        Returns:
            The persisted node keys, edge keys and log position.
        """
        ...


def submit_or_raise[ErrorT: Exception](
    submitter: GroupSubmitter, group: MutationGroup, error_type: type[ErrorT]
) -> GroupOutcome:
    """Submit `group`; return its outcome, or raise `error_type` if it did not take effect.

    `applied` and `unchanged` (every mutation was already in the graph) are success. A group
    that is committed but not yet applied (FalkorDB stayed unreachable) is a failure here: the
    run must not go on to read or extend a graph that does not hold its data yet. The data is
    safe in the log, the gateway applies it on recovery and a re-run is idempotent.

    Args:
        submitter: The gateway seam used to submit.
        group: The mutation group to submit.
        error_type: Exception class raised on failure; built with a single message argument.

    Returns:
        The group's `GroupOutcome` (status `applied` or `unchanged`).

    Side Effects:
        Logs the group to the graph log and applies it to the graph.

    Raises:
        ErrorT: the gateway refused the group or could not reach its stores, or the group was
            committed but not applied. The message is `FalkorDB write failed: <sanitized text>`
            and the gateway error, if any, is chained as `__cause__`.
    """
    try:
        outcome = submitter.submit_group(group)
    except _GATEWAY_FAILURES as exc:
        raise error_type(f"{_WRITE_FAILED_PREFIX}{exc}") from exc
    if outcome.status == "committed_apply_pending":
        raise error_type(f"{_WRITE_FAILED_PREFIX}{_COMMITTED_NOT_APPLIED_MESSAGE}")
    return outcome


def require_caught_up[ErrorT: Exception](
    submitter: GroupSubmitter, graph: str, error_type: type[ErrorT]
) -> None:
    """Raise `error_type` unless `graph` holds everything committed to its log.

    A check on a writer's own write (the reachability check) is only valid once the graph has
    caught up with the log: if an earlier group of the graph is still waiting to be applied,
    the check would read a graph that does not hold the data yet. The data is safe in the log;
    the gateway applies it on recovery and a re-run is idempotent.

    Args:
        submitter: The gateway seam used to ask.
        graph: Name of the graph to check.
        error_type: Exception class raised on failure; built with a single message argument.

    Raises:
        ErrorT: the graph is behind its log, or the log could not be read to tell. The message
            is `FalkorDB write failed: ...` and a gateway error, if any, is chained.
    """
    try:
        caught_up = submitter.is_caught_up(graph)
    except _GATEWAY_FAILURES as exc:
        raise error_type(f"{_WRITE_FAILED_PREFIX}{exc}") from exc
    if not caught_up:
        raise error_type(f"{_WRITE_FAILED_PREFIX}{_COMMITTED_NOT_APPLIED_MESSAGE}")


def read_persisted_or_raise[ErrorT: Exception](
    submitter: GroupSubmitter, graph: str, error_type: type[ErrorT]
) -> PersistedElements:
    """Read the node keys, edge keys and log position of `graph`; writes and logs nothing.

    Args:
        submitter: The gateway seam used to read.
        graph: Name of the graph to read.
        error_type: Exception class raised on failure; built with a single message argument.

    Returns:
        The persisted node keys, edge keys and log position of `graph`.

    Raises:
        ErrorT: `graph` could not be read, or answered a row that cannot be classified. The
            message is `FalkorDB write failed: ...` (the read serves a write) and the cause is
            chained.
    """
    try:
        return submitter.read_persisted_elements(graph)
    except _GATEWAY_FAILURES as exc:
        raise error_type(f"{_WRITE_FAILED_PREFIX}{exc}") from exc


def stamp_legacy_edges_before_first_write[ErrorT: Exception](
    submitter: GroupSubmitter, graph: str, audit_event_id: str | None, error_type: type[ErrorT]
) -> EdgeIdentityBackfillOutcome:
    """Run the idempotent edge-identity backfill on `graph` before a writer's first group.

    A graph written before the gateway holds edges with no `identity`; the gateway merges edges
    on it, so submitting onto such a graph would add a parallel edge beside every legacy one.
    Every ingestion submit path calls this once per target graph, after its
    own pre-flight validation and before the first group, so a rejected request writes and logs
    nothing. A graph with nothing to stamp (fresh, or already stamped) costs one read and logs
    nothing to the graph log; otherwise one stamp group is logged under `audit_event_id`, the
    same audit event as the submit.

    Args:
        submitter: The gateway seam used to stamp.
        graph: Name of the target graph.
        audit_event_id: Audit event the stamp group is logged under, or None.
        error_type: Exception class raised on failure; built with a single message argument.

    Returns:
        The backfill outcome: `applied` means one stamp group was logged on `graph`.

    Side Effects:
        May log and apply one stamp group on `graph`.

    Raises:
        ErrorT: the graph could not be read, the stamp group was refused or could not be logged.
            The message is `FalkorDB write failed: <sanitized text>`, the cause is chained.
    """
    try:
        return submitter.stamp_legacy_edge_identity(graph, audit_event_id)
    except _GATEWAY_FAILURES as exc:
        raise error_type(f"{_WRITE_FAILED_PREFIX}{exc}") from exc
