"""The Policy Lifecycle side of the Graph Write Gateway seam.

Policy Lifecycle issues no write Cypher. A writer turns its decision into gateway primitives and
submits them as ONE group on the `policy_system` graph, through a `LifecycleSubmitter`.
`GraphWriteTarget` carries what a writer needs for that: the submitter and the graph the group goes
to. Unlike the other writers' targets it carries no audit event: a lifecycle call makes several
groups with different anchors (the `applied` row of each transition, none for a draft edit or a
backfill), so the anchor is passed per submit. `submit_group` is the one place a writer's
primitives reach the gateway:

- an empty tuple submits nothing (a group needs at least one primitive);
- the graph's legacy, identity-less edges are stamped once per target, before its first group (a
  graph written before the gateway would otherwise gain a parallel edge beside every legacy one); a
  guarded flow stamps explicitly with `ensure_stamped`, and the call is then a no-op;
- the group is submitted (`submit_or_raise`) and the graph must be caught up before the writer
  returns (`require_caught_up`), because a lifecycle transition reads back what it wrote.

Failures reach callers as `PolicyLifecycleGraphUnavailableError("FalkorDB write failed: ...")`,
whether the gateway refused the group, could not reach its stores, or the group was committed but
not applied. The one failure kept apart is a stale `ExpectedPosition`: it is raised as
`StaleGroupError` so a guarded caller can name it.

Guarded flows (fork repoint, capability-claim create) follow this order, the order of
`ingestion/adapters/internal_seed/persist.py`: `require_caught_up`, then read the log position P0
with `read_log_position`, then the guard reads and the Python decision (a refusal here logs nothing,
not even a stamp), then `ensure_stamped` (the stamp only adds `stamped` entries after P0), then
submit with `ExpectedPosition(P0 + stamped)`. Reading P0 AFTER the guard reads would make the guard
vacuous.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import ValidationError

from ps_service.graph_gateway.errors import (
    GraphApplyError,
    GraphLogPersistenceError,
    GraphUnavailableError,
    GraphWriteRejectedError,
    StaleGraphStateError,
    UnexpectedGraphReplyError,
)
from ps_service.graph_gateway.models import MutationGroup
from ps_service.ingestion.graph_submission import (
    GroupSubmitter,
    stamp_legacy_edges_before_first_write,
    submit_or_raise,
)
from ps_service.ingestion.graph_submission import (
    require_caught_up as _require_caught_up,
)
from ps_service.persistence import StatePostgresConnectionError
from ps_service.policy_lifecycle.errors import PolicyLifecycleGraphUnavailableError, StaleGroupError

if TYPE_CHECKING:
    from collections.abc import Generator

    from ps_service.graph_gateway.models import Precondition, Primitive

__all__ = [
    "GraphWriteTarget",
    "LifecycleSubmitter",
    "edge_identity",
    "ensure_stamped",
    "lifecycle_submitter",
    "read_log_position",
    "refuse_unstorable_values",
    "require_caught_up",
    "submit_group",
]

_WRITE_FAILED_PREFIX = "FalkorDB write failed: "
_UNSTORABLE_MESSAGE = "a value cannot be stored in the graph (rejected before anything was written)"

_GATEWAY_FAILURES = (
    GraphWriteRejectedError,
    GraphApplyError,
    GraphUnavailableError,
    GraphLogPersistenceError,
    UnexpectedGraphReplyError,
    StatePostgresConnectionError,
)
"""Every failure a gateway call raises on purpose; anything else is a bug and propagates."""


@runtime_checkable
class LifecycleSubmitter(GroupSubmitter, Protocol):
    """What Policy Lifecycle needs of the gateway: a `GroupSubmitter` that reads the log position.

    Declared here, not on `GroupSubmitter`, so no other writer's submitter (or test fake) has to
    grow a method it does not use. `GraphWriteGateway` satisfies it structurally. The group
    submission methods are those of `GroupSubmitter`; this Protocol adds `read_log_position`.
    """

    def read_log_position(self, graph: str) -> int:
        """Read the last log position of `graph`, the position an `ExpectedPosition` names.

        Args:
            graph: The name of the graph whose log is read.

        Returns:
            The last log position of the graph; `0` when nothing is logged.

        Raises:
            GraphReplayGatedError: the graph is closed by a pending or failed replay.
            GraphLogUnavailableError: the state database stayed unreachable through the gateway's
                retry budget.

        Side Effects:
            None; reading the position writes nothing.
        """
        ...


@contextmanager
def refuse_unstorable_values() -> Generator[None]:
    """Map a primitive's own validation failure to the persistence error, echoing no content.

    A writer builds gateway primitives before it submits them, and a primitive refuses a property
    value the graph cannot store (a reserved key, an empty or nested list, a non-finite number).
    Wrapping the construction in this context manager refuses such a value before anything is
    written, as the same error a refused group is. The `ValidationError`, which carries the
    offending content, is chained as `__cause__` and never put in the message.

    Yields:
        None; the wrapped block builds primitives.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, a primitive built in
            the block failed validation. Other exceptions from the block propagate unchanged.

    Side Effects:
        None.
    """
    try:
        yield
    except ValidationError as exc:
        raise PolicyLifecycleGraphUnavailableError(
            f"{_WRITE_FAILED_PREFIX}{_UNSTORABLE_MESSAGE}"
        ) from exc


def lifecycle_submitter(gateway: GroupSubmitter) -> LifecycleSubmitter:
    """Narrow the process's gateway to what Policy Lifecycle needs of it.

    `graph_write_access` hands out a `GroupSubmitter`; the guarded flows also read the log
    position. The real `GraphWriteGateway` has `read_log_position`; a gateway without it is a
    wiring error, not a runtime condition, so it is a `TypeError`, not a persistence error.

    Args:
        gateway: The process's installed graph write gateway.

    Returns:
        `gateway` itself, typed as a `LifecycleSubmitter`.

    Raises:
        TypeError: `gateway` has no `read_log_position` method.

    Side Effects:
        None.
    """
    if not isinstance(gateway, LifecycleSubmitter):
        message = "the installed graph write gateway cannot read the log position"
        raise TypeError(message)
    return gateway


class GraphWriteTarget:
    """Where a writer's group goes: a submitter and a graph name (the anchor is per submit).

    Attributes:
        submitter: The gateway submitter groups are sent through.
        graph_name: The name of the graph the groups go to.
        stamped: Whether this target's graph has had its legacy edges stamped by this target.
    """

    def __init__(self, submitter: LifecycleSubmitter, graph_name: str) -> None:
        """Hold the submitter and the target graph; nothing is stamped yet.

        Args:
            submitter: The gateway submitter groups are sent through.
            graph_name: The name of the graph the groups go to.
        """
        self.submitter = submitter
        self.graph_name = graph_name
        self.stamped = False


def edge_identity(edge_type: str, source_id: str, target_id: str) -> str:
    """Return the gateway identity of an edge, `{type}:{source}->{target}`.

    The scheme of the Domain Mapper writer, the internal-seed path, Company Merge and the
    edge-identity backfill, so one edge has the same identity whichever path wrote it.

    Args:
        edge_type: The edge's type, for example `"GOVERNED_BY"`.
        source_id: The id of the edge's source node.
        target_id: The id of the edge's target node.

    Returns:
        The identity string, unique per (type, source, target).

    Side Effects:
        None.
    """
    return f"{edge_type}:{source_id}->{target_id}"


def require_caught_up(target: GraphWriteTarget) -> None:
    """Raise unless `target`'s graph holds everything committed to its log.

    Args:
        target: The gateway target whose graph is checked.

    Returns:
        None when the graph is caught up.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the graph is behind its
            log or the log could not be read to tell.

    Side Effects:
        Reads the graph's log position and applied position; nothing is written.
    """
    _require_caught_up(target.submitter, target.graph_name, PolicyLifecycleGraphUnavailableError)


def ensure_stamped(target: GraphWriteTarget, audit_event_id: str | None) -> int:
    """Stamp `target`'s legacy identity-less edges once per target; return how many were stamped.

    The graph must be caught up first (the scan must see what the log wrote). A graph with nothing
    to stamp costs one read and logs nothing; otherwise one stamp group is logged under
    `audit_event_id`. A second call on the same target does nothing.

    Args:
        target: The gateway target whose graph is stamped; its `stamped` flag is set afterwards.
        audit_event_id: The audit row the stamp group is linked to, or `None` for no audit link.

    Returns:
        The number of edges stamped; `0` when the target was already stamped or nothing needed it.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the graph is behind its
            log, or the gateway refused or failed the stamp group.

    Side Effects:
        Reads the graph; may submit one stamp group to the gateway; sets `target.stamped`.
    """
    if target.stamped:
        return 0
    require_caught_up(target)
    outcome = stamp_legacy_edges_before_first_write(
        target.submitter, target.graph_name, audit_event_id, PolicyLifecycleGraphUnavailableError
    )
    target.stamped = True
    return outcome.stamped


def read_log_position(target: GraphWriteTarget) -> int:
    """Read the last log position of `target`'s graph (no node or edge scan, nothing logged).

    Args:
        target: The gateway target whose graph log is read.

    Returns:
        The position of the last entry in the graph's mutation log.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the log could not be
            read or the graph is closed to the gateway.

    Side Effects:
        None; nothing is written.
    """
    try:
        return target.submitter.read_log_position(target.graph_name)
    except _GATEWAY_FAILURES as exc:
        raise PolicyLifecycleGraphUnavailableError(f"{_WRITE_FAILED_PREFIX}{exc}") from exc


def submit_group(
    target: GraphWriteTarget,
    primitives: tuple[Primitive, ...],
    *,
    audit_event_id: str | None,
    preconditions: tuple[Precondition, ...] = (),
) -> None:
    """Submit `primitives` as one group on `target`'s graph and require it applied.

    Nothing to write submits nothing and stamps nothing. Otherwise the graph's legacy edges are
    stamped before the first group of this target (a no-op when a guarded flow already did), then
    the group is submitted and the graph must be caught up.

    Args:
        target: The gateway target the group goes to.
        primitives: The mutation primitives of the group; an empty tuple submits nothing.
        audit_event_id: The audit row the group is linked to, or `None` for no audit link.
        preconditions: Conditions the gateway checks before logging the group, for example an
            `ExpectedPosition`; empty by default.

    Returns:
        None; on return the group is applied to the graph.

    Raises:
        StaleGroupError: an `ExpectedPosition` precondition no longer held; nothing was logged.
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: <sanitized text>` if the
            gateway refused the group, could not reach its stores, or the group was committed but
            not applied, or if the graph was not caught up before or after. A gateway error, if
            any, is chained as `__cause__`.

    Side Effects:
        May submit a stamp group first, then submits one mutation group, which the gateway logs
        and applies to the graph.
    """
    if not primitives:
        return
    group = MutationGroup(
        graph=target.graph_name,
        audit_event_id=audit_event_id,
        primitives=primitives,
        preconditions=preconditions,
    )
    ensure_stamped(target, audit_event_id)
    try:
        submit_or_raise(target.submitter, group, PolicyLifecycleGraphUnavailableError)
    except PolicyLifecycleGraphUnavailableError as exc:
        if isinstance(exc.__cause__, StaleGraphStateError):
            raise StaleGroupError from exc.__cause__
        raise
    require_caught_up(target)
