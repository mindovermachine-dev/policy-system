"""The Restore side of the Graph Write Gateway seam.

Restore issues no write Cypher. It hands the gateway one group for the whole instrument
(native, baseline and `policy_system`) and learns how it ended. This module is the one place
a gateway failure becomes a Restore error:

- `StaleGraphStateError` is not translated: the `policy_system` leg carries `ExpectedPosition`
  and a refusal means another writer moved the graph since Restore read it, which the caller's
  bounded retry loop answers by reading again;
- any other refusal is `RestoreGroupRejectedError`;
- an unreachable log or FalkorDB, or an entry FalkorDB refused, is `RestoreGraphWriteFailedError`;
- a group that is committed but not applied to every graph is `RestoreApplyPendingError`
  (raised, not returned, like every migrated writer: a restore must not report success for a
  graph that does not hold the instrument yet).

The gateway's messages are fixed and sanitized (no host, port, query or content), so they are
safe to carry in the translated errors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ps_service.graph_gateway.errors import (
    GraphApplyError,
    GraphLogPersistenceError,
    GraphUnavailableError,
    GraphWriteRejectedError,
    StaleGraphStateError,
    UnexpectedGraphReplyError,
)
from ps_service.graph_gateway.models import MultiGraphGroup
from ps_service.logging.facade import emit_log_entry
from ps_service.persistence import StatePostgresConnectionError
from ps_service.restore.errors import (
    RestoreApplyPendingError,
    RestoreGraphWriteFailedError,
    RestoreGroupRejectedError,
)

if TYPE_CHECKING:
    from ps_service.graph_gateway.models import (
        CatchUpResult,
        GroupOutcome,
        MultiGraphOutcome,
        MutationGroup,
    )
    from ps_service.logging.emitter import LogEmitter

__all__ = [
    "MAX_POLICY_SYSTEM_ATTEMPTS",
    "RestoreGateway",
    "as_restore_gateway",
    "ensure_caught_up",
    "read_log_position",
    "submit_restore",
]

MAX_POLICY_SYSTEM_ATTEMPTS = 3
"""How many times a restore reads `policy_system` and submits before it gives up."""

_COMPONENT = "restore"
_CATCH_UP_ACTION = "catch_up_policy_system"
_WRITE_FAILED_PREFIX = "FalkorDB write failed: "
_COMMITTED_NOT_APPLIED_MESSAGE = (
    "the restore was committed to the graph log but could not be applied to every graph yet; "
    "it will be applied on recovery"
)
_GATEWAY_FAILURES = (
    GraphApplyError,
    GraphLogPersistenceError,
    GraphUnavailableError,
    UnexpectedGraphReplyError,
    StatePostgresConnectionError,
)
"""Every failure a gateway call raises on purpose; anything else is a bug and propagates."""


@runtime_checkable
class RestoreGateway(Protocol):
    """What Restore needs of the Graph Write Gateway: read a position, submit a group.

    Structural protocol, so any gateway that offers these calls can take a restore. Each call
    may raise the gateway's own errors; `submit_restore`, `ensure_caught_up` and
    `read_log_position` translate them.
    """

    def submit_multi_graph_group(self, group: MultiGraphGroup) -> MultiGraphOutcome:
        """Log and apply `group` (several graphs, one transaction).

        Args:
            group: The group to log and apply.

        Returns:
            The outcome, whose `status` says whether the group was applied.
        """
        ...

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        """Log and apply `group` (one graph).

        Args:
            group: The group to log and apply.

        Returns:
            The outcome, whose `status` says whether the group was applied.
        """
        ...

    def read_log_position(self, graph: str) -> int:
        """Read the last log position of `graph` (what an `ExpectedPosition` is made from).

        Args:
            graph: Name of the graph.

        Returns:
            The position of the graph's last logged entry.
        """
        ...

    def is_caught_up(self, graph: str) -> bool:
        """Answer True only if every committed entry of `graph` is applied to it.

        Args:
            graph: Name of the graph.

        Returns:
            Whether the graph holds everything committed to its log.
        """
        ...

    def catch_up(self, graph: str) -> CatchUpResult:
        """Apply every logged entry of `graph` that its applied marker does not cover.

        Args:
            graph: Name of the graph.

        Returns:
            Whether the graph is now caught up with its log.
        """
        ...


def as_restore_gateway(gateway: object) -> RestoreGateway:
    """Narrow the installed gateway to a `RestoreGateway`.

    Args:
        gateway: The object the process installed as its Graph Write Gateway.

    Returns:
        `gateway` itself, typed as a `RestoreGateway`.

    Raises:
        TypeError: the installed gateway lacks a call Restore needs (a wiring error, not a
            runtime condition).
    """
    if not isinstance(gateway, RestoreGateway):
        message = "the installed graph write gateway cannot take a restore"
        raise TypeError(message)
    return gateway


def ensure_caught_up(
    gateway: RestoreGateway, graph: str, *, emitter: LogEmitter | None = None
) -> None:
    """Make `graph` hold everything committed to its log before Restore reads it.

    An earlier group that was committed but not applied (FalkorDB went away) would otherwise
    leave Restore reading a graph that does not yet hold it. Applies the pending entries first
    (idempotent) and writes nothing new.

    Args:
        gateway: The gateway to ask and, if behind, to catch up.
        graph: Name of the graph about to be read.
        emitter: Log emitter for the catch-up log entry; `None` uses the default emitter.

    Returns:
        None; returns normally only when the graph holds everything committed to its log.

    Side Effects:
        When the graph is behind, applies its pending log entries to it and emits one
        `catch_up_policy_system` log entry (`caught_up` or `behind`).

    Raises:
        RestoreGraphWriteFailedError: the log could not be read, FalkorDB is unreachable or
            refuses an entry, or the graph is still behind its log. The cause is chained.
    """
    try:
        if not gateway.is_caught_up(graph):
            result = gateway.catch_up(graph)
            emit_log_entry(
                component=_COMPONENT,
                action=_CATCH_UP_ACTION,
                outcome="caught_up" if result.caught_up else "behind",
                extra={"graph": graph},
                emitter=emitter,
            )
            if not result.caught_up:
                raise RestoreGraphWriteFailedError(
                    f"{_WRITE_FAILED_PREFIX}{_COMMITTED_NOT_APPLIED_MESSAGE}"
                )
    except _GATEWAY_FAILURES as exc:
        raise RestoreGraphWriteFailedError(f"{_WRITE_FAILED_PREFIX}{exc}") from exc


def read_log_position(gateway: RestoreGateway, graph: str) -> int:
    """Read the last log position of `graph`, the position a guarded leg is built against.

    Args:
        gateway: The gateway to read the log from.
        graph: Name of the graph.

    Returns:
        The position of the graph's last logged entry.

    Raises:
        RestoreGraphWriteFailedError: the log could not be read. The cause is chained.
    """
    try:
        return gateway.read_log_position(graph)
    except _GATEWAY_FAILURES as exc:
        raise RestoreGraphWriteFailedError(f"{_WRITE_FAILED_PREFIX}{exc}") from exc


def submit_restore(
    gateway: RestoreGateway,
    group: MultiGraphGroup | MutationGroup,
) -> None:
    """Submit the restore's group; return when it took effect (applied, or nothing to change).

    Args:
        gateway: The gateway to submit to.
        group: The restore's group: a multi-graph group, or a plain group when only one leg has
            content.

    Returns:
        None; returns normally only when the group is applied.

    Side Effects:
        Logs the group to the graph log and applies it to the target graph(s).

    Raises:
        StaleGraphStateError: a guarded leg's graph moved since it was read; nothing was logged
            (not translated: the caller retries).
        RestoreGroupRejectedError: the gateway refused the group; nothing was logged.
        RestoreGraphWriteFailedError: the log or FalkorDB failed or refused.
        RestoreApplyPendingError: committed to the log but not yet applied to every graph.
    """
    try:
        if isinstance(group, MultiGraphGroup):
            outcome_status = gateway.submit_multi_graph_group(group).status
        else:
            outcome_status = gateway.submit_group(group).status
    except StaleGraphStateError:
        raise
    except GraphWriteRejectedError as exc:
        raise RestoreGroupRejectedError(f"{_WRITE_FAILED_PREFIX}{exc}") from exc
    except _GATEWAY_FAILURES as exc:
        raise RestoreGraphWriteFailedError(f"{_WRITE_FAILED_PREFIX}{exc}") from exc
    if outcome_status == "committed_apply_pending":
        raise RestoreApplyPendingError(f"{_WRITE_FAILED_PREFIX}{_COMMITTED_NOT_APPLIED_MESSAGE}")
