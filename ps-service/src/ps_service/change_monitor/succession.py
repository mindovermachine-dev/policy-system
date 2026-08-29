"""`SUPERSEDED_BY` + status succession bookkeeping against a `{short}_native` graph.

The five small graph operations `trigger_reingestion` composes
(PLAN_REVIEWED.md §1.4, §2 "succession.py"). Every read and write goes
through the local `_execute_query`, an exact copy of
`ps_service.ingestion.graph_writer._execute_query`: a
`redis.exceptions.RedisError` is wrapped in `SuccessionPersistenceError` and
FalkorDB is marked unhealthy in `ps_service.dependency_health`, self-healing
on the next successful call.

Atomicity (PLAN_REVIEWED.md §0, flaw 2): `link_and_supersede` is a *single*
fused Cypher statement -- the `SUPERSEDED_BY` edge and `prior.status =
'superseded'` are written together, so no edge-without-status sub-state can
ever exist. `find_prior_instrument` uses the deterministic lookup that stays
unambiguous even mid-crash-window (it excludes the new node and any node
already superseded into it).

`SUPERSEDED_BY` and `RegulatoryInstrument` are fixed module constants,
interpolated into the query strings as literals only -- they are schema
identifiers, never externally sourced, so no allow-list check applies
(contrast `graph_writer._upsert_node`'s adapter-supplied labels).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import redis.exceptions

from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    SuccessionPersistenceError,
)
from ps_service.change_monitor.models import PriorInstrument
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy

if TYPE_CHECKING:
    from ps_service.change_monitor.falkordb_client import GraphHandle, GraphQueryResult

_REGULATORY_INSTRUMENT = "RegulatoryInstrument"
_SUPERSEDED_BY = "SUPERSEDED_BY"

_FIND_PRIOR_QUERY = f"""\
MATCH (n:{_REGULATORY_INSTRUMENT})
WHERE n.status = 'active'
  AND n.id <> $new_id
  AND NOT (n)-[:{_SUPERSEDED_BY}]->(:{_REGULATORY_INSTRUMENT} {{id: $new_id}})
RETURN n.id AS id, n.instrument_type AS instrument_type"""

_NEW_NODE_EXISTS_QUERY = (
    f"MATCH (n:{_REGULATORY_INSTRUMENT} {{id: $new_id}}) RETURN n.status AS status"
)

_SUCCESSION_COMPLETE_QUERY = f"""\
MATCH (prior:{_REGULATORY_INSTRUMENT} {{status: 'superseded'}})-[:{_SUPERSEDED_BY}]->
      (new:{_REGULATORY_INSTRUMENT} {{id: $new_id}})
RETURN prior.id AS prior_id"""

_SET_VERSION_QUERY = (
    f"MATCH (n:{_REGULATORY_INSTRUMENT} {{id: $new_id}}) SET n.version = $new_version"
)

_FUSED_SUCCESSION_QUERY = f"""\
MATCH (prior:{_REGULATORY_INSTRUMENT} {{id: $prior_id}}),
      (new:{_REGULATORY_INSTRUMENT} {{id: $new_id}})
MERGE (prior)-[:{_SUPERSEDED_BY}]->(new)
SET prior.status = 'superseded'"""


def _execute_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """The one call site every `graph.query()` read/write in this module goes through.

    Wraps `redis.exceptions.RedisError` -- the base class every
    connection/timeout error the `falkordb`/`redis-py` stack raises
    subclasses -- into `SuccessionPersistenceError`, records the outage in
    `ps_service.dependency_health` for `/ready`'s live signal, and
    self-heals on the next successful call. Exact copy of
    `ps_service.ingestion.graph_writer._execute_query`.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise SuccessionPersistenceError(f"FalkorDB succession write failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return result


def _rows(result: GraphQueryResult) -> list[list[object]]:
    """Recover the row-of-columns shape a real FalkorDB `result_set` has."""
    return cast("list[list[object]]", result.result_set)


def find_prior_instrument(graph: GraphHandle, new_id: str) -> PriorInstrument:
    """Return the single active prior `RegulatoryInstrument` `new_id` supersedes.

    Runs the deterministic lookup (PLAN_REVIEWED.md §0): the
    `status='active'` node that is neither `new_id` itself nor already
    superseded into `new_id`. Exactly one such node exists outside a crash
    window; raises `ChangeMonitorStateError` on 0 rows (no active prior) or
    >1 rows (a genuinely inconsistent graph).
    """
    rows = _rows(_execute_query(graph, _FIND_PRIOR_QUERY, {"new_id": new_id}))
    if not rows:
        raise ChangeMonitorStateError(
            f"no active prior RegulatoryInstrument to supersede for new id {new_id!r}"
        )
    if len(rows) > 1:
        ids = ", ".join(str(row[0]) for row in rows)
        raise ChangeMonitorStateError(
            f"multiple active prior RegulatoryInstruments for new id {new_id!r}: {ids}"
        )
    identifier, instrument_type = rows[0]
    return PriorInstrument(id=str(identifier), instrument_type=str(instrument_type))


def new_node_exists(graph: GraphHandle, new_id: str) -> str | None:
    """Return the `status` of the `new_id` node, or `None` when it does not exist."""
    rows = _rows(_execute_query(graph, _NEW_NODE_EXISTS_QUERY, {"new_id": new_id}))
    if not rows:
        return None
    return str(rows[0][0])


def is_succession_complete(graph: GraphHandle, new_id: str) -> str | None:
    """Return the prior id when succession into `new_id` is already complete.

    The completed-succession probe: a `superseded` prior with a
    `SUPERSEDED_BY` edge into `new_id`. Returns that prior's id, or `None`
    when no completed edge exists (drives `trigger_reingestion`'s
    `already_processed` short-circuit).
    """
    rows = _rows(_execute_query(graph, _SUCCESSION_COMPLETE_QUERY, {"new_id": new_id}))
    if not rows:
        return None
    return str(rows[0][0])


def set_new_version_property(graph: GraphHandle, new_id: str, new_version: str) -> None:
    """Write `new_version` onto the `new_id` node's `version` property.

    Resolution A (PLAN_REVIEWED.md §1.2): Ingestion always stores
    `version='1.0'`, so `trigger_reingestion` owns this post-ingest
    bookkeeping SET. Parameterized and idempotent.
    """
    _execute_query(graph, _SET_VERSION_QUERY, {"new_id": new_id, "new_version": new_version})


def link_and_supersede(graph: GraphHandle, prior_id: str, new_id: str) -> None:
    """Write the `SUPERSEDED_BY` edge and `prior.status='superseded'` in one statement.

    THE fused succession write (PLAN_REVIEWED.md §0, flaw 2): edge + status
    change together, so no edge-without-status window can exist. `MERGE` +
    `SET` are both idempotent, so re-running against an already-superseded
    prior is a clean no-op.
    """
    _execute_query(graph, _FUSED_SUCCESSION_QUERY, {"prior_id": prior_id, "new_id": new_id})
