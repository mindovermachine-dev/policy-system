"""The Company Merge side of the Graph Write Gateway seam.

Company Merge issues no write Cypher. A writer turns its decision into gateway primitives and
submits them as one group on a graph, under the run's audit event, through a `GroupSubmitter`.
`GraphWriteTarget` carries what a writer needs for that: the submitter, the graph the group goes to
and the audit event it is logged under. `submit_group` is the one place a writer's primitives
reach the gateway:

- an empty tuple submits nothing (a group needs at least one primitive);
- the graph's legacy, identity-less edges are stamped once per target, before its first group
  (a graph written before the gateway would otherwise gain a parallel edge beside every legacy
  one);
- the group is submitted (`submit_or_raise`) and the graph must be caught up before the writer
  returns (`require_caught_up`), because the merge goes on to read what it just wrote.

Failures reach callers as `CompanyMergePersistenceError("FalkorDB write failed: ...")`, whether the
gateway refused the group, could not reach its stores, or the group was committed but not applied.
`read_rows` is the read wrapper: it records FalkorDB reachability in `ps_service.dependency_health`
(write-side health is the gateway's) and maps a failed read to `FalkorDB query failed: ...`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import redis.exceptions

from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.graph_gateway.models import MutationGroup
from ps_service.ingestion.graph_submission import (
    require_caught_up,
    stamp_legacy_edges_before_first_write,
    submit_or_raise,
)

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_gateway.models import Primitive
    from ps_service.ingestion.graph_submission import GroupSubmitter

__all__ = ["GraphWriteTarget", "edge_identity", "read_rows", "submit_group"]


class GraphWriteTarget:
    """Where a writer's group goes: a submitter, a graph name and the run's audit event id.

    Attributes:
        submitter: The gateway entry point groups are submitted through.
        graph_name: Name of the graph every group of this target is written to.
        audit_event_id: Id of the run's audit event; every group is logged under it.
        stamped: Whether this target's graph has had its legacy edges stamped by this run.
    """

    def __init__(self, submitter: GroupSubmitter, graph_name: str, audit_event_id: str) -> None:
        """Hold the submitter, the target graph and the audit event; nothing is stamped yet.

        Args:
            submitter: The gateway entry point groups are submitted through.
            graph_name: Name of the graph to write to.
            audit_event_id: Id of the audit event the groups are logged under.
        """
        self.submitter = submitter
        self.graph_name = graph_name
        self.audit_event_id = audit_event_id
        self.stamped = False
        """Whether this target's graph has had its legacy edges stamped by this run."""


def edge_identity(edge_type: str, source_id: str, target_id: str) -> str:
    """Return `{type}:{source}->{target}`, the gateway identity of an edge.

    There is one identity per (type, endpoints). The Domain Mapper writer and the internal-seed
    path use the same scheme, so one instrument yields the same edges whichever path wrote them.

    Args:
        edge_type: Relationship type name of the edge.
        source_id: Id of the source node.
        target_id: Id of the target node.

    Returns:
        The identity string, for example `OBLIGATES:a->b`.
    """
    return f"{edge_type}:{source_id}->{target_id}"


def submit_group(
    target: GraphWriteTarget,
    primitives: tuple[Primitive, ...],
    *,
    checkpoint_requested: bool = False,
) -> None:
    """Submit `primitives` as one group on `target`'s graph and require it applied.

    Nothing to write submits nothing and stamps nothing. Otherwise the graph's legacy edges are
    stamped before the first group of this target, then the group is submitted and the graph must
    be caught up before this returns.

    Args:
        target: The submitter, graph and audit event the group goes to.
        primitives: The gateway primitives of the group; an empty tuple means nothing to write.
        checkpoint_requested: Asks the gateway to record the graph's digest once the group is
            applied. A writer sets it on its last group to the graph; a failure to record never
            fails the group.

    Returns:
        None. The group is applied and the graph caught up when this returns.

    Side Effects:
        Submits one mutation group through the gateway; on the first non-empty call per target,
        first stamps the graph's legacy edges and sets `target.stamped`.

    Raises:
        CompanyMergePersistenceError: `FalkorDB write failed: <sanitized text>` if the gateway
            refused the group, could not reach its stores, or the group was committed but not
            applied.
    """
    if not primitives:
        return
    group = MutationGroup(
        graph=target.graph_name,
        audit_event_id=target.audit_event_id,
        primitives=primitives,
        checkpoint_requested=checkpoint_requested,
    )
    if not target.stamped:
        stamp_legacy_edges_before_first_write(
            target.submitter, target.graph_name, target.audit_event_id, CompanyMergePersistenceError
        )
        target.stamped = True
    submit_or_raise(target.submitter, group, CompanyMergePersistenceError)
    require_caught_up(target.submitter, target.graph_name, CompanyMergePersistenceError)


def read_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    """Run a fixed read `query` on `graph`; return its rows, recording FalkorDB reachability.

    Args:
        graph: The FalkorDB graph handle to read from.
        query: A read-only Cypher query string; values go in `params`, never into the text.
        params: Query parameters, or None for none.

    Returns:
        The result-set rows, one list per row; empty when the query matches nothing.

    Side Effects:
        Marks FalkorDB healthy on success, or unhealthy on a Redis error, in
        `ps_service.dependency_health`.

    Raises:
        CompanyMergePersistenceError: `FalkorDB query failed: <error>` if the read could not be
            made.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise CompanyMergePersistenceError(f"FalkorDB query failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return cast("list[list[object]]", result.result_set)
