"""`SUPERSEDED_BY` + status succession bookkeeping against a `{short}_native` graph.

The small graph operations `trigger_reingestion` composes
(PLAN_REVIEWED.md §1.4, §2 "succession.py"). Every read and write goes
through the local `_execute_query`, an exact copy of
`ps_service.ingestion.graph_writer._execute_query`: a
`redis.exceptions.RedisError` is wrapped in `SuccessionPersistenceError` and
FalkorDB is marked unhealthy in `ps_service.dependency_health`, self-healing
on the next successful call.

Atomicity (PLAN_REVIEWED.md §0, flaw 2): `link_and_supersede` is a *single*
fused Cypher statement -- the `SUPERSEDED_BY` edge, `absorbed` and `prior.status =
'superseded'` are written together, so no edge-without-status sub-state can
ever exist. The whole succession is split into three idempotent writes,
in order: `link_and_supersede` (native: edge, `absorbed`, status, marker `linked`),
`supersede_in_single_tenant` (`policy_system`: edge + status), `clear_marker`. A crash
between them leaves the `linked` marker, which classifies as `finalize` and resumes
at step 2. `find_prior_instrument` uses the deterministic lookup that stays
unambiguous even mid-crash-window (it excludes the new node and any node
already superseded into it).

`SUPERSEDED_BY` and `RegulatoryInstrument` are fixed module constants,
interpolated into the query strings as literals only -- they are schema
identifiers, never externally sourced, so no allow-list check applies
(contrast `graph_writer._upsert_node`'s adapter-supplied labels).
"""

from __future__ import annotations

from dataclasses import dataclass
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

# Operational bookkeeping label (see domain_schema.vocabulary_exceptions.OPERATIONAL_LABELS):
# one marker node per in-flight re-ingest, holding the last completed stage.
_REINGEST_PROGRESS = "ReingestProgress"

# The 'linked' literal below equals models.LINKED. It is inlined, not interpolated: an f-string
# placeholder in Cypher counts as a dynamic site in the vocabulary scan's pinned totals.
_FUSED_SUCCESSION_QUERY = f"""\
MATCH (prior:{_REGULATORY_INSTRUMENT} {{id: $prior_id}}),
      (new:{_REGULATORY_INSTRUMENT} {{id: $new_id}})
MERGE (prior)-[e:{_SUPERSEDED_BY}]->(new)
SET e.absorbed = true, prior.status = 'superseded'
WITH new
MERGE (m:{_REINGEST_PROGRESS} {{id: $new_id}})
SET m.stage = 'linked'"""

_REINGESTION_FACTS_QUERY = f"""\
MATCH (n:{_REGULATORY_INSTRUMENT} {{id: $new_id}})
OPTIONAL MATCH (p:{_REGULATORY_INSTRUMENT})-[e:{_SUPERSEDED_BY}]->(n)
OPTIONAL MATCH (m:{_REINGEST_PROGRESS} {{id: $new_id}})
RETURN p.id AS prior_id, p.instrument_type AS prior_instrument_type,
       p.status AS prior_status, e.absorbed AS absorbed, m.stage AS stage"""

_MARK_STAGE_QUERY = f"""\
MERGE (m:{_REINGEST_PROGRESS} {{id: $new_id}})
SET m.stage = $stage"""

# The `policy_system` side of the succession (D2). Runs on the single-tenant handle:
# the merged graph is what the tracked set (`status = 'active'`) and `ps-list-ingested` read.
_SINGLE_TENANT_SUPERSEDE_QUERY = f"""\
MATCH (prior:{_REGULATORY_INSTRUMENT} {{id: $prior_id}}),
      (new:{_REGULATORY_INSTRUMENT} {{id: $new_id}})
MERGE (prior)-[:{_SUPERSEDED_BY}]->(new)
SET prior.status = 'superseded'
RETURN prior.id AS prior_id"""

_CLEAR_MARKER_QUERY = f"MATCH (m:{_REINGEST_PROGRESS} {{id: $new_id}}) DELETE m"


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


@dataclass(frozen=True, slots=True)
class ReingestionFacts:
    """What the `{short}_native` graph says about one new-version id (read-only, one query).

    `node_exists` is false when the new `RegulatoryInstrument` node is absent (zero
    rows). `prior_id` / `prior_instrument_type` / `prior_status` describe the node
    holding a `SUPERSEDED_BY` edge into the new one (all `None` without an edge);
    `absorbed` is that edge's property (`None` when unset, i.e. a legacy
    ingestion-only link). `marker_stage` is the `ReingestProgress` marker's last
    completed stage, or `None`.
    """

    node_exists: bool
    prior_id: str | None
    prior_instrument_type: str | None
    prior_status: str | None
    absorbed: bool | None
    marker_stage: str | None


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def read_reingestion_facts(graph: GraphHandle, new_id: str) -> ReingestionFacts:
    """Read the node / incoming-edge / marker facts for `new_id` in one query.

    Raises `ChangeMonitorStateError` when more than one node is superseded into
    `new_id` (a genuinely inconsistent graph).
    """
    rows = _rows(_execute_query(graph, _REINGESTION_FACTS_QUERY, {"new_id": new_id}))
    if not rows:
        return ReingestionFacts(
            node_exists=False,
            prior_id=None,
            prior_instrument_type=None,
            prior_status=None,
            absorbed=None,
            marker_stage=None,
        )
    priors = [row for row in rows if row[0] is not None]
    if len(priors) > 1:
        ids = ", ".join(str(row[0]) for row in priors)
        raise ChangeMonitorStateError(f"multiple nodes superseded into {new_id!r}: {ids}")
    row = priors[0] if priors else rows[0]
    prior_id, prior_type, prior_status, absorbed, stage = row
    return ReingestionFacts(
        node_exists=True,
        prior_id=_optional_str(prior_id),
        prior_instrument_type=_optional_str(prior_type),
        prior_status=_optional_str(prior_status),
        absorbed=None if absorbed is None else bool(absorbed),
        marker_stage=_optional_str(stage),
    )


def mark_stage_complete(graph: GraphHandle, new_id: str, stage: str) -> None:
    """Upsert the `ReingestProgress` marker for `new_id` to `stage` (idempotent).

    Called only after `stage` returned, so the marker is a durable "this stage is
    done" fact in the store the succession lives in. Operational bookkeeping, not
    UC-4 content; deleted when the succession completes.
    """
    _execute_query(graph, _MARK_STAGE_QUERY, {"new_id": new_id, "stage": stage})


def clear_marker(graph: GraphHandle, new_id: str) -> None:
    """Delete the `ReingestProgress` marker for `new_id` (a clean no-op when absent)."""
    _execute_query(graph, _CLEAR_MARKER_QUERY, {"new_id": new_id})


def link_and_supersede(graph: GraphHandle, prior_id: str, new_id: str) -> None:
    """Write the native-graph succession in one statement: edge, `absorbed`, status, marker.

    THE fused succession write (PLAN_REVIEWED.md §0, flaw 2): the `SUPERSEDED_BY`
    edge, `e.absorbed = true`, `prior.status = 'superseded'` and the
    `ReingestProgress` marker moved to `linked` land together, so no
    edge-without-status or edge-without-absorbed window can exist. The marker
    is deleted afterwards by :func:`clear_marker`. `MERGE` + `SET` are idempotent,
    so re-running is a clean no-op.
    """
    _execute_query(graph, _FUSED_SUCCESSION_QUERY, {"prior_id": prior_id, "new_id": new_id})


def supersede_in_single_tenant(single_tenant: GraphHandle, prior_id: str, new_id: str) -> None:
    """Write the succession into the merged `policy_system` graph (step 2 of 3, D2).

    `MERGE (prior)-[:SUPERSEDED_BY]->(new)` and `prior.status = 'superseded'` on the
    single-tenant handle, so the tracked set (`status = 'active'`) stops revisiting the prior and
    `ps-list-ingested` shows `superseded_by`. Idempotent. Zero matched rows means the prior or
    the new node is missing from `policy_system` (Company Merge did not land it), an
    inconsistent graph: raises `ChangeMonitorStateError` and writes nothing, leaving the native
    `linked` marker in place so the next sweep retries.
    """
    rows = _rows(
        _execute_query(
            single_tenant,
            _SINGLE_TENANT_SUPERSEDE_QUERY,
            {"prior_id": prior_id, "new_id": new_id},
        )
    )
    if not rows:
        raise ChangeMonitorStateError(
            f"policy_system has no {prior_id!r} and {new_id!r} pair to link with SUPERSEDED_BY"
        )
