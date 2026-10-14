"""The Regulatory Change Monitor side of the Graph Write Gateway seam.

The re-ingest succession issues no write Cypher. A writer in `succession.py` turns one step of the
succession into gateway primitives and `submit_succession_group` submits them as ONE group on one
graph, anchored to an audit row. That function is the one place a succession write reaches the
gateway and the one place a gateway failure becomes a Change Monitor error:

- a group that names a node the graph does not hold (a gateway `MissingTargetError`) is
  `ChangeMonitorStateError`: the graph is inconsistent and nothing of the group was logged;
- every other refusal or unreachable store, and a group that was committed but could not be applied
  yet (`committed_apply_pending`), is `SuccessionPersistenceError` with the gateway's sanitized
  message. A pending group is a failure here: the `linked` marker (or the earlier stage markers) in
  the log makes the next sweep resume where this one stopped, once the graph has caught up;
- after each group the graph must hold everything committed to its log (`require_caught_up`), so a
  later read of the graph never runs ahead of an earlier pending group.

`SuccessionTarget` carries what a writer needs: the submitter, the two graph names (the same
formulas the sweep's openers use) and the audit anchor. The anchor is a callable, memoised by its
caller, because a re-ingest that writes only the succession (`finalize`, link-only) records its
`ingestion_run.submit` audit row lazily, just before the first group, and one that writes nothing
(`already_processed`) never calls it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import ValidationError

from ps_service.change_monitor.errors import ChangeMonitorStateError, SuccessionPersistenceError
from ps_service.graph_gateway.errors import (
    GraphApplyError,
    GraphLogPersistenceError,
    GraphUnavailableError,
    MissingTargetError,
    UnexpectedGraphReplyError,
)
from ps_service.graph_gateway.models import MutationGroup
from ps_service.ingestion.graph_submission import (
    GroupSubmitter,
    require_caught_up,
    submit_or_raise,
)
from ps_service.logging import emit_log_entry
from ps_service.persistence import StatePostgresConnectionError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.graph_gateway.models import CatchUpResult, GroupOutcome, Primitive
    from ps_service.logging import LogEmitter

__all__ = [
    "CatchUpSubmitter",
    "SuccessionTarget",
    "edge_identity",
    "ensure_caught_up",
    "submit_succession_group",
]

_COMPONENT = "change_monitor"
_SUBMIT_ACTION = "submit_succession_group"
_CATCH_UP_ACTION = "catch_up_native_graph"
_WRITE_FAILED_PREFIX = "FalkorDB write failed: "
_CATCH_UP_FAILURES = (
    GraphApplyError,
    GraphLogPersistenceError,
    GraphUnavailableError,
    UnexpectedGraphReplyError,
    StatePostgresConnectionError,
)
"""Every failure a catch-up raises on purpose; anything else is a bug and propagates."""


@runtime_checkable
class CatchUpSubmitter(GroupSubmitter, Protocol):
    """What the sweep needs of the gateway before it reads: a `GroupSubmitter` that can catch up.

    Declared here, not on `GroupSubmitter`, so no other writer's submitter (or test fake) has to
    grow a method it does not use. `GraphWriteGateway` satisfies it structurally.
    """

    def catch_up(self, graph: str) -> CatchUpResult:
        """Apply every logged entry of `graph` that its applied marker does not cover.

        Args:
            graph: Name of the graph to catch up.

        Returns:
            A `CatchUpResult`; `caught_up` is false when the graph is still behind.

        Raises:
            GraphLogUnavailableError: the graph log could not be read.
            GraphApplyError: the graph database refuses an entry; the graph stays blocked.

        Side Effects:
            Applies the pending entries to the graph database and advances the graph's applied
            marker.
        """
        ...


@dataclass(frozen=True, slots=True)
class SuccessionTarget:
    """Where the succession's groups go: a submitter, the two graphs and the audit anchor.

    Attributes:
        submitter: Gateway submitter that commits and applies each group.
        native_graph: Name of the instrument's native graph.
        single_tenant_graph: Name of the merged single-tenant graph.
        audit_anchor: Callable returning the audit event id every group is anchored to.
        emitter: Log emitter for the submit entries; `None` uses the default emitter.
    """

    submitter: GroupSubmitter
    native_graph: str
    single_tenant_graph: str
    audit_anchor: Callable[[], str]
    """The `ingestion_run.submit` audit event id; called once per group, memoised by the caller."""
    emitter: LogEmitter | None = None


def ensure_caught_up(
    submitter: GroupSubmitter, graph: str, *, emitter: LogEmitter | None = None
) -> None:
    """Make `graph` hold everything committed to its log before a re-ingest reads it.

    A succession group that was committed but could not be applied (FalkorDB went away) leaves the
    stage marker or the `linked` marker in the log only. The classification of the next sweep
    reads the graph, so it applies the pending entries first. The catch-up is idempotent and has
    no concurrency guard: the sweep assumes one sweep per caller.

    Args:
        submitter: Gateway submitter; must be a `CatchUpSubmitter`.
        graph: Name of the graph to bring up to date.
        emitter: Log emitter for the catch-up entry; `None` uses the default emitter.

    Raises:
        SuccessionPersistenceError: the log could not be read, FalkorDB is still unreachable or
            refuses an entry, so the graph is behind its log. The cause is chained.
        TypeError: `submitter` cannot catch up (a wiring error, not a runtime condition).

    Side Effects:
        When the graph is behind, applies its pending logged entries and emits one
        `catch_up_native_graph` log entry. Nothing is written when it is already caught up.
    """
    if not isinstance(submitter, CatchUpSubmitter):
        message = "the installed graph write gateway cannot catch a graph up"
        raise TypeError(message)
    try:
        if not submitter.is_caught_up(graph):
            result = submitter.catch_up(graph)
            emit_log_entry(
                component=_COMPONENT,
                action=_CATCH_UP_ACTION,
                outcome="caught_up" if result.caught_up else "behind",
                extra={"graph": graph},
                emitter=emitter,
            )
    except _CATCH_UP_FAILURES as exc:
        raise SuccessionPersistenceError(f"{_WRITE_FAILED_PREFIX}{exc}") from exc
    require_caught_up(submitter, graph, SuccessionPersistenceError)


def edge_identity(edge_type: str, source_id: str, target_id: str) -> str:
    """Return `{type}:{source}->{target}`, the gateway identity of an edge.

    The scheme of every other gateway writer and of the edge-identity backfill, so one edge has the
    same identity whichever path wrote it. A deliberate near-duplicate (pinned equal to Graph
    Cleanup's by a test), not a cross-component import.

    Args:
        edge_type: Edge type name.
        source_id: Id of the edge's source node.
        target_id: Id of the edge's target node.

    Returns:
        The identity string, one per (type, endpoints).
    """
    return f"{edge_type}:{source_id}->{target_id}"


def submit_succession_group(
    target: SuccessionTarget,
    graph: str,
    *,
    step: str,
    new_id: str,
    build: Callable[[], tuple[Primitive, ...]],
    missing: str,
    checkpoint_requested: bool = False,
) -> GroupOutcome:
    """Submit one succession step as one group on `graph`; return the gateway's outcome.

    This is the one place a succession write reaches the gateway and a gateway failure becomes
    a Change Monitor error.

    Args:
        target: Submitter, graph names and audit anchor for the succession.
        graph: Name of the graph the group is submitted on.
        step: Short label of the succession step, used in the log entry.
        new_id: Id of the new instrument, used as the log entry's `entity_id`.
        build: Makes the primitives of the group. One the graph cannot store (a reserved key, a
            non-finite number) is refused here as a persistence error, before anything is logged.
        missing: Message of the `ChangeMonitorStateError` raised when the group names a node
            `graph` does not hold.
        checkpoint_requested: Asks the gateway to record the graph's digest once the group is
            applied; only a re-ingest's terminal step on a graph sets it.

    Returns:
        The gateway's `GroupOutcome` for the committed and applied group.

    Raises:
        ChangeMonitorStateError: a node the group names is missing; nothing was written.
        SuccessionPersistenceError: the group was refused, the stores were unreachable, the group
            was committed but not applied, or the graph is behind its log.
        AuditTrailUnavailableError: the audit anchor could not be recorded; nothing was written.

    Side Effects:
        Commits one mutation group to the gateway log and applies it to `graph`; calls
        `target.audit_anchor`; emits one `submit_succession_group` log entry.
    """
    try:
        primitives = build()
        group = MutationGroup(
            graph=graph,
            audit_event_id=target.audit_anchor(),
            primitives=primitives,
            checkpoint_requested=checkpoint_requested,
        )
    except ValidationError as exc:
        _log(target, graph, step, new_id, outcome="failed", extra={"error_type": "ValidationError"})
        message = "the succession write holds a value the graph cannot store"
        raise SuccessionPersistenceError(message) from exc
    try:
        outcome = submit_or_raise(target.submitter, group, SuccessionPersistenceError)
        require_caught_up(target.submitter, graph, SuccessionPersistenceError)
    except SuccessionPersistenceError as exc:
        cause = exc.__cause__
        _log(
            target,
            graph,
            step,
            new_id,
            outcome="failed",
            extra={"error_type": type(cause).__name__ if cause is not None else "NotApplied"},
        )
        if isinstance(cause, MissingTargetError):
            raise ChangeMonitorStateError(missing) from cause
        raise
    _log(target, graph, step, new_id, outcome=outcome.status, extra={"primitives": len(primitives)})
    return outcome


def _log(
    target: SuccessionTarget,
    graph: str,
    step: str,
    new_id: str,
    *,
    outcome: str,
    extra: dict[str, object],
) -> None:
    emit_log_entry(
        component=_COMPONENT,
        action=_SUBMIT_ACTION,
        entity_id=new_id,
        outcome=outcome,
        extra={"graph": graph, "step": step, **extra},
        emitter=target.emitter,
    )
