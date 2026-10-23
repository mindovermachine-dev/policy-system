"""The Graph Cleanup side of the Graph Write Gateway seam.

Graph Cleanup issues no write Cypher. A writer turns a guarded decision into gateway primitives and
submits them as ONE group on the configured single-tenant graph, through a `CleanupSubmitter`.
`GraphWriteTarget` carries what a writer needs for that: the submitter and the graph the group goes
to. `submit_cleanup_group` is the one place a writer's primitives reach the gateway and the one
place a gateway failure becomes a Graph Cleanup error:

- a stale `ExpectedPosition` is `GraphCleanupStaleStateError` (the flow's own message): the graph
  moved between the guard reads and the append, so nothing of this group was logged;
- every other gateway failure (a refused group, an unreachable store or graph, an unstorable value,
  no gateway installed) is `GraphCleanupPersistenceError` with one fixed, sanitized message; the
  gateway error is chained as `__cause__` and never put in the message;
- a group that was `applied`, `unchanged`, or committed but not applied yet
  (`committed_apply_pending`) returns normally. The last is SUCCESS: the edit is in the log and the
  gateway applies it on recovery, so no `failed` audit row is written for it. This is why Graph
  Cleanup does not use `ingestion.graph_submission.submit_or_raise`, which treats a pending group
  as a failure.

Guarded flows follow this order: the `applied` audit row, then `require_caught_up`, then read the
log position P0 with `read_log_position`, then the guard reads and the Python decision (a guard
miss or a read failure logs nothing, not even a stamp), then `ensure_stamped` (the stamp only adds
`stamped` entries after P0), then submit with `ExpectedPosition(P0 + stamped)`. Reading P0 AFTER
the guard reads would make the guard vacuous. A refused position happens after the stamp, so a
identity-stamp group may already be in the log under the `applied` row's audit id; the cleanup
group itself is never logged.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import ValidationError

from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
)
from ps_service.graph_gateway.errors import (
    GraphApplyError,
    GraphLogPersistenceError,
    GraphUnavailableError,
    GraphWriteRejectedError,
    StaleGraphStateError,
    UnexpectedGraphReplyError,
)
from ps_service.graph_gateway.models import MutationGroup
from ps_service.ingestion.graph_submission import GroupSubmitter
from ps_service.logging import emit_log_entry
from ps_service.persistence import StatePostgresConnectionError

if TYPE_CHECKING:
    from collections.abc import Generator

    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.graph_gateway.models import Precondition, Primitive

__all__ = [
    "CleanupSubmitter",
    "GraphWriteTarget",
    "cleanup_submitter",
    "edge_identity",
    "ensure_stamped",
    "graph_write_target",
    "read_log_position",
    "refuse_unstorable_values",
    "require_caught_up",
    "submit_cleanup_group",
]

_COMPONENT = "graph_cleanup"
_WRITE_FAILED_MESSAGE = "the policy graph database could not complete the write"
_STALE_DEFAULT_MESSAGE = "the graph changed since the preview; nothing was written"

_GATEWAY_FAILURES = (
    GraphWriteRejectedError,
    GraphApplyError,
    GraphUnavailableError,
    GraphLogPersistenceError,
    UnexpectedGraphReplyError,
    StatePostgresConnectionError,
    GraphWriteUnavailableError,
)
"""Every failure a gateway call raises on purpose; anything else is a bug and propagates.

`GraphWriteRejectedError` is the base of `StaleGraphStateError`, so a handler that catches both
must name the stale one first (`_mapped_failures`).
"""


@runtime_checkable
class CleanupSubmitter(GroupSubmitter, Protocol):
    """What Graph Cleanup needs of the gateway: a `GroupSubmitter` that reads the log position.

    Declared here, not on `GroupSubmitter`, so no other writer's submitter (or test fake) has to
    grow a method it does not use. `GraphWriteGateway` satisfies it structurally.
    """

    def read_log_position(self, graph: str) -> int:
        """Read the last log position of `graph` (the position an `ExpectedPosition` names).

        Args:
            graph: Name of the graph whose log is read.

        Returns:
            The last committed log position.
        """
        ...


def cleanup_submitter(gateway: GroupSubmitter) -> CleanupSubmitter:
    """Narrow the process's gateway to what Graph Cleanup needs of it.

    `graph_write_access` hands out a `GroupSubmitter`; the guarded flows also read the log
    position. The real `GraphWriteGateway` has `read_log_position`; a gateway without it is a
    wiring error, not a runtime condition, so it is a `TypeError`, not a persistence error.

    Args:
        gateway: The installed gateway, as handed out by `graph_write_access`.

    Returns:
        The same object, typed as a `CleanupSubmitter`.

    Raises:
        TypeError: `gateway` has no `read_log_position`.
    """
    if not isinstance(gateway, CleanupSubmitter):
        message = "the installed graph write gateway cannot read the log position"
        raise TypeError(message)
    return gateway


class GraphWriteTarget:
    """Where a writer's group goes: a submitter and a graph name.

    Attributes:
        submitter: The gateway the group is submitted through.
        graph_name: Name of the graph the group is written to.
        stamped: Whether `ensure_stamped` has already run for this target.
    """

    def __init__(self, submitter: CleanupSubmitter, graph_name: str) -> None:
        """Hold the submitter and the target graph; nothing is stamped yet.

        Args:
            submitter: The gateway the group is submitted through.
            graph_name: Name of the graph the group is written to; non-empty.
        """
        self.submitter = submitter
        self.graph_name = graph_name
        self.stamped = False
        """Whether this target's graph has had its identity-less edges stamped."""


def graph_write_target(
    dependencies: GraphCleanupDependencies, config: ServiceConfig
) -> GraphWriteTarget:
    """The submitter and graph a cleanup write goes to, resolved at call time.

    The graph name is `config.falkordb_graph`, the one the reads' opener selects, so a write
    never lands in a different graph than the one it was guarded against.

    Args:
        dependencies: Holder of the lazily resolved graph write gateway.
        config: Service configuration passed to the gateway resolver; names the graph.

    Returns:
        A `GraphWriteTarget` for the single-tenant graph, not yet stamped.

    Raises:
        GraphCleanupPersistenceError: no gateway is installed.
    """
    with _mapped_failures(_STALE_DEFAULT_MESSAGE):
        submitter = dependencies.graph_write_gateway(config)
    return GraphWriteTarget(submitter, config.falkordb_graph)


def edge_identity(edge_type: str, source_id: str, target_id: str) -> str:
    """Return `{type}:{source}->{target}`: the gateway identity of an edge.

    There is one identity per (type, endpoints). The scheme is shared with the Domain Mapper
    writer, the internal-seed path, Company Merge, Policy Lifecycle and the edge-identity
    backfill, so one edge has the same identity whichever path wrote it. It is a deliberate
    near-duplicate of Policy Lifecycle's (pinned equal by a test), not a cross-component import.

    Args:
        edge_type: Relationship type, for example `GOVERNED_BY`.
        source_id: Id of the edge's source node.
        target_id: Id of the edge's target node.

    Returns:
        The identity string.
    """
    return f"{edge_type}:{source_id}->{target_id}"


@contextmanager
def _mapped_failures(stale_message: str) -> Generator[None]:
    """Map the gateway's failures to Graph Cleanup's errors (stale first: it is a subclass)."""
    try:
        yield
    except StaleGraphStateError as exc:
        raise GraphCleanupStaleStateError(stale_message) from exc
    except _GATEWAY_FAILURES as exc:
        _log_write_failure(exc)
        raise GraphCleanupPersistenceError(_WRITE_FAILED_MESSAGE) from exc


def _log_write_failure(exc: Exception) -> None:
    """Emit a `failed` log entry carrying only the exception type, never its message."""
    emit_log_entry(
        component=_COMPONENT,
        action="submit_cleanup_group",
        outcome="failed",
        extra={"error_type": type(exc).__name__},
    )


@contextmanager
def refuse_unstorable_values() -> Generator[None]:
    """Map a primitive's own validation failure to the persistence error (no content echoed).

    A writer builds gateway primitives before it submits them, and a primitive refuses a property
    value the graph cannot store (a reserved key, an empty or nested list, a non-finite number).
    Such a value is refused before anything is written, as the same error a refused group is.
    The `ValidationError` (which carries the offending content) is chained, never put in the
    message.

    Side Effects:
        Emits a `failed` log entry with the error type when a value is refused.

    Raises:
        GraphCleanupPersistenceError: the fixed, sanitized write-failed message.
    """
    try:
        yield
    except ValidationError as exc:
        _log_write_failure(exc)
        raise GraphCleanupPersistenceError(_WRITE_FAILED_MESSAGE) from exc


def require_caught_up(target: GraphWriteTarget) -> None:
    """Raise unless `target`'s graph holds everything committed to its log.

    A guard read is only valid once the graph has caught up with the log: if an earlier group of
    the graph is still waiting to be applied, the read would not see it.

    Args:
        target: The graph to check.

    Side Effects:
        Emits a `failed` log entry when the graph is behind its log.

    Raises:
        GraphCleanupPersistenceError: the graph is behind its log, or the log could not be read.
    """
    with _mapped_failures(_STALE_DEFAULT_MESSAGE):
        caught_up = target.submitter.is_caught_up(target.graph_name)
    if not caught_up:
        emit_log_entry(
            component=_COMPONENT,
            action="require_caught_up",
            outcome="failed",
            extra={"reason": "graph_behind_its_log"},
        )
        raise GraphCleanupPersistenceError(_WRITE_FAILED_MESSAGE)


def read_log_position(target: GraphWriteTarget) -> int:
    """Read the last log position of `target`'s graph (no node or edge scan, nothing logged).

    Args:
        target: The graph whose log is read.

    Returns:
        The last committed log position, the value an `ExpectedPosition` precondition names.

    Raises:
        GraphCleanupPersistenceError: the log could not be read or the graph is closed to the
            gateway.
    """
    with _mapped_failures(_STALE_DEFAULT_MESSAGE):
        return target.submitter.read_log_position(target.graph_name)


def ensure_stamped(target: GraphWriteTarget, audit_event_id: str | None) -> int:
    """Stamp `target`'s identity-less edges once per target; return how many were stamped.

    The graph must be caught up first (the scan must see what the log wrote). A graph with nothing
    to stamp costs one read and logs nothing; otherwise one stamp group is logged under
    `audit_event_id` (`None`: no audit link). A second call on the same target does nothing.

    Args:
        target: The graph to stamp; its `stamped` flag is set once the stamp succeeded.
        audit_event_id: Audit row linked to the stamp group; `None` for no link.

    Returns:
        The number of edges stamped; 0 when there was nothing to stamp or the target was
        already stamped.

    Side Effects:
        May submit one stamp group to the gateway log and sets `target.stamped`.

    Raises:
        GraphCleanupPersistenceError: the graph could not be read or the stamp group was refused.
    """
    if target.stamped:
        return 0
    require_caught_up(target)
    with _mapped_failures(_STALE_DEFAULT_MESSAGE):
        outcome = target.submitter.stamp_legacy_edge_identity(target.graph_name, audit_event_id)
    target.stamped = True
    return outcome.stamped


def submit_cleanup_group(
    target: GraphWriteTarget,
    primitives: tuple[Primitive, ...],
    *,
    audit_event_id: str | None,
    stale_message: str,
    preconditions: tuple[Precondition, ...] = (),
) -> None:
    """Submit `primitives` as one group on `target`'s graph; return once it is committed.

    Nothing to write submits nothing and stamps nothing. Otherwise the graph's identity-less
    edges are stamped before the first group of this target (a no-op when a guarded flow already
    did) and the group is submitted. A group that is committed but not applied yet is success; no
    catch-up is awaited after it.

    Every cleanup operation is exactly one group, so every group is the terminal one and asks the
    gateway to record the graph's digest once it is applied; a failure to record never fails the
    group.

    Args:
        target: The graph and gateway the group goes to.
        primitives: The graph edits of the group; an empty tuple submits nothing.
        audit_event_id: Audit row the group is linked to; `None` for no link.
        stale_message: Message of the stale-state error raised when a precondition no longer
            holds.
        preconditions: Conditions the graph must still satisfy, typically an
            `ExpectedPosition`; empty by default.

    Returns:
        None. A normal return means the group is committed, applied or not yet.

    Side Effects:
        May submit a stamp group, then submits the cleanup group to the gateway log, and
        emits a `succeeded` log entry with the outcome status and graph name.

    Raises:
        GraphCleanupStaleStateError: an `ExpectedPosition` precondition no longer held; nothing
            of this group was logged. The message is `stale_message`.
        GraphCleanupPersistenceError: the gateway refused the group or could not reach its
            stores; the fixed, sanitized message, with the gateway error chained.
    """
    if not primitives:
        return
    with refuse_unstorable_values():
        group = MutationGroup(
            graph=target.graph_name,
            audit_event_id=audit_event_id,
            primitives=primitives,
            preconditions=preconditions,
            checkpoint_requested=True,
        )
    ensure_stamped(target, audit_event_id)
    with _mapped_failures(stale_message):
        outcome = target.submitter.submit_group(group)
    emit_log_entry(
        component=_COMPONENT,
        action="submit_cleanup_group",
        outcome="succeeded",
        extra={"status": outcome.status, "graph": target.graph_name},
    )
