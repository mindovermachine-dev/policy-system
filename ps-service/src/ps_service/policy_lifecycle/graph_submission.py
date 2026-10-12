"""The Policy Lifecycle side of the Graph Write Gateway seam (issue #211).

Policy Lifecycle issues no write Cypher. A writer turns its decision into gateway primitives and
submits them as ONE group on the `policy_system` graph, through a `LifecycleSubmitter`.
`GraphWriteTarget` carries what a writer needs for that: the submitter and the graph the group goes
to. Unlike the other writers' targets it carries no audit event: a lifecycle call makes several
groups with different anchors (the `applied` row of each transition, none for a draft edit or a
backfill), so the anchor is passed per submit. `submit_group` is the one place a writer's
primitives reach the gateway:

- an empty tuple submits nothing (a group needs at least one primitive);
- the graph's legacy, identity-less edges are stamped once per target, before its first group
  (a graph written before the gateway would otherwise gain a parallel edge beside every legacy
  one, #208 F1); a guarded flow stamps explicitly with `ensure_stamped`, and the call is then a
  no-op;
- the group is submitted (`submit_or_raise`) and the graph must be caught up before the writer
  returns (`require_caught_up`), because a lifecycle transition reads back what it wrote.

Failures reach callers as `PolicyLifecycleGraphUnavailableError("FalkorDB write failed: ...")`,
whether the gateway refused the group, could not reach its stores, or the group was committed but
not applied (the documented C-0 exception of #202). The one failure kept apart is a stale
`ExpectedPosition`: it is raised as `StaleGroupError` so a guarded caller can name it.

Guarded flows (fork repoint, capability-claim create) follow this order, the order of
`ingestion/adapters/internal_seed/persist.py`: `require_caught_up`, then read the log position P0
with `read_log_position`, then the guard reads and the Python decision (a refusal here logs nothing,
not even a stamp), then `ensure_stamped` (the stamp only adds `stamped` entries after P0), then
submit with `ExpectedPosition(P0 + stamped)`. Reading P0 AFTER the guard reads would make the guard
vacuous.
"""

from __future__ import annotations

import os
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
    "single_tenant_graph_name",
    "submit_group",
]

_WRITE_FAILED_PREFIX = "FalkorDB write failed: "
_DEFAULT_SINGLE_TENANT_GRAPH_NAME = "policy_system"
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
    grow a method it does not use. `GraphWriteGateway` satisfies it structurally.
    """

    def read_log_position(self, graph: str) -> int:
        """Read the last log position of `graph` (the position an `ExpectedPosition` names)."""
        ...


@contextmanager
def refuse_unstorable_values() -> Generator[None]:
    """Map a primitive's own validation failure to the persistence error (no content echoed).

    A writer builds gateway primitives before it submits them, and a primitive refuses a property
    value the graph cannot store (a reserved key, an empty or nested list, a non-finite number).
    The direct writer accepted such a value and let FalkorDB decide; now it is refused before
    anything is written, as the same error a refused group is. The `ValidationError` (which
    carries the offending content) is chained, never put in the message.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
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
    """
    if not isinstance(gateway, LifecycleSubmitter):
        message = "the installed graph write gateway cannot read the log position"
        raise TypeError(message)
    return gateway


def single_tenant_graph_name() -> str:
    """The single-tenant policy graph's name: `PS_FALKORDB_GRAPH`, default `"policy_system"`.

    This component's own accessor for the convention `mcp_interface.mcp_server` and Company Merge
    share (a deliberate near-duplicate, not a shared import): the one graph name used for the
    read-only query surface and the gateway's writes.
    """
    return os.environ.get("PS_FALKORDB_GRAPH", _DEFAULT_SINGLE_TENANT_GRAPH_NAME)


class GraphWriteTarget:
    """Where a writer's group goes: a submitter and a graph name (the anchor is per submit)."""

    def __init__(self, submitter: LifecycleSubmitter, graph_name: str) -> None:
        """Hold the submitter and the target graph; nothing is stamped yet."""
        self.submitter = submitter
        self.graph_name = graph_name
        self.stamped = False
        """Whether this target's graph has had its legacy edges stamped by this run."""


def edge_identity(edge_type: str, source_id: str, target_id: str) -> str:
    """`{type}:{source}->{target}`: the gateway identity of an edge, one per (type, endpoints).

    The scheme of the Domain Mapper writer, the internal-seed path, Company Merge and the
    edge-identity backfill, so one edge has the same identity whichever path wrote it.
    """
    return f"{edge_type}:{source_id}->{target_id}"


def require_caught_up(target: GraphWriteTarget) -> None:
    """Raise unless `target`'s graph holds everything committed to its log.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the graph is behind its
            log or the log could not be read to tell.
    """
    _require_caught_up(target.submitter, target.graph_name, PolicyLifecycleGraphUnavailableError)


def ensure_stamped(target: GraphWriteTarget, audit_event_id: str | None) -> int:
    """Stamp `target`'s legacy identity-less edges once per target; return how many were stamped.

    The graph must be caught up first (the scan must see what the log wrote). A graph with nothing
    to stamp costs one read and logs nothing; otherwise one stamp group is logged under
    `audit_event_id` (`None`: no audit link). A second call on the same target does nothing.

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`.
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

    Raises:
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: ...`, the log could not be
            read or the graph is closed to the gateway.
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

    Raises:
        StaleGroupError: an `ExpectedPosition` precondition no longer held; nothing was logged.
        PolicyLifecycleGraphUnavailableError: `FalkorDB write failed: <sanitized text>` if the
            gateway refused the group, could not reach its stores, or the group was committed but
            not applied. A gateway error, if any, is chained as `__cause__`.
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
