"""`SUPERSEDED_BY` + status succession bookkeeping against a `{short}_native` graph.

The small graph operations `trigger_reingestion` composes (PLAN_REVIEWED.md §1.4, §2
"succession.py"). The READS (`find_prior_instrument`, `new_node_exists`, `is_succession_complete`,
`read_reingestion_facts`) go through the local `_execute_query`, an exact copy of
`ps_service.ingestion.graph_writer._execute_query`: a `redis.exceptions.RedisError` is wrapped in
`SuccessionPersistenceError` and FalkorDB is marked unhealthy in `ps_service.dependency_health`,
self-healing on the next successful call.

The WRITES issue no Cypher (issue #213). Each is a Graph Write Gateway group built from primitives
and submitted by `graph_submission.submit_succession_group`, which also owns the failure mapping
(a missing node is `ChangeMonitorStateError`, anything else `SuccessionPersistenceError`) and the
FalkorDB health record (the gateway records it). The succession is several groups, because a
group covers one graph: `set_new_version_property` (native, after stage 1), `mark_stage_complete`
(native, one per stage), `link_and_supersede` (native: edge, `absorbed`, status, marker `linked`,
together or not at all, so no edge-without-status sub-state can ever exist),
`supersede_in_single_tenant` (`policy_system`: edge + status) and `clear_marker` (native). A crash
between the last three leaves the `linked` marker, which classifies as `finalize` and resumes at
the `policy_system` step. `find_prior_instrument` uses the deterministic lookup that stays
unambiguous even mid-crash-window (it excludes the new node and any node already superseded into
it).

`SUPERSEDED_BY` and `RegulatoryInstrument` are fixed module constants, interpolated into the read
query strings as literals only -- they are schema identifiers, never externally sourced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import redis.exceptions

from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    SuccessionPersistenceError,
)
from ps_service.change_monitor.graph_submission import (
    SuccessionTarget,
    edge_identity,
    submit_succession_group,
)
from ps_service.change_monitor.models import LINKED, PriorInstrument
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.graph_gateway.models import (
    DeleteNode,
    MergeProperty,
    NodeRef,
    StampEdgeIdentity,
    UpsertEdge,
    UpsertNode,
)

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

# Operational bookkeeping label (see domain_schema.vocabulary_exceptions.OPERATIONAL_LABELS):
# one marker node per in-flight re-ingest, holding the last completed stage.
_REINGEST_PROGRESS = "ReingestProgress"

_REINGESTION_FACTS_QUERY = f"""\
MATCH (n:{_REGULATORY_INSTRUMENT} {{id: $new_id}})
OPTIONAL MATCH (p:{_REGULATORY_INSTRUMENT})-[e:{_SUPERSEDED_BY}]->(n)
OPTIONAL MATCH (m:{_REINGEST_PROGRESS} {{id: $new_id}})
RETURN p.id AS prior_id, p.instrument_type AS prior_instrument_type,
       p.status AS prior_status, e.absorbed AS absorbed, m.stage AS stage"""


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


def set_new_version_property(target: SuccessionTarget, new_id: str, new_version: str) -> None:
    """Write `new_version` onto the `new_id` node's `version` property, as one native group.

    Resolution A (PLAN_REVIEWED.md §1.2): Ingestion always stores `version='1.0'`, so
    `trigger_reingestion` owns this post-ingest bookkeeping write (issue #213: a `MergeProperty`
    group on the native graph). Idempotent: an unchanged version logs nothing. Raises
    `ChangeMonitorStateError`, writing nothing, when the node is absent.
    """
    submit_succession_group(
        target,
        target.native_graph,
        step="version",
        new_id=new_id,
        build=lambda: (
            MergeProperty(
                label=_REGULATORY_INSTRUMENT, id=new_id, properties={"version": new_version}
            ),
        ),
        missing=f"the native graph has no {new_id!r} node to set the version on",
    )


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


def mark_stage_complete(target: SuccessionTarget, new_id: str, stage: str) -> None:
    """Upsert the `ReingestProgress` marker for `new_id` to `stage`, as one native group.

    Called only after `stage` returned, so the marker is a durable "this stage is done" fact in
    the store the succession lives in (issue #213: an `UpsertNode` group on the native graph, one
    per stage). Operational bookkeeping, not UC-4 content; deleted when the succession completes.
    Idempotent: re-marking the same stage logs nothing.
    """
    submit_succession_group(
        target,
        target.native_graph,
        step="stage_marker",
        new_id=new_id,
        build=lambda: (
            UpsertNode(label=_REINGEST_PROGRESS, id=new_id, properties={"stage": stage}),
        ),
        missing=f"the native graph could not record the {stage!r} stage of {new_id!r}",
    )


def clear_marker(target: SuccessionTarget, new_id: str) -> None:
    """Delete the `ReingestProgress` marker for `new_id`, as one native group (a no-op if absent).

    Issue #213: a `DeleteNode` group on the native graph. A marker that is not there logs nothing.
    """
    submit_succession_group(
        target,
        target.native_graph,
        step="clear_marker",
        new_id=new_id,
        build=lambda: (DeleteNode(label=_REINGEST_PROGRESS, id=new_id),),
        missing=f"the native graph could not clear the marker of {new_id!r}",
    )


def _instrument_ref(node_id: str) -> NodeRef:
    return NodeRef(label=_REGULATORY_INSTRUMENT, id=node_id)


def _stamp_superseded_by(prior_id: str, new_id: str) -> StampEdgeIdentity:
    return StampEdgeIdentity(
        type=_SUPERSEDED_BY,
        identity=edge_identity(_SUPERSEDED_BY, prior_id, new_id),
        source=_instrument_ref(prior_id),
        target=_instrument_ref(new_id),
    )


def _upsert_superseded_by(prior_id: str, new_id: str, properties: dict[str, object]) -> UpsertEdge:
    return UpsertEdge(
        type=_SUPERSEDED_BY,
        identity=edge_identity(_SUPERSEDED_BY, prior_id, new_id),
        source=_instrument_ref(prior_id),
        target=_instrument_ref(new_id),
        properties=properties,
    )


def link_and_supersede(target: SuccessionTarget, prior_id: str, new_id: str) -> None:
    """Write the native-graph succession as ONE gateway group: edge, `absorbed`, status, marker.

    THE fused succession write (PLAN_REVIEWED.md §0, flaw 2; issue #213): in order, stamp any
    legacy `SUPERSEDED_BY` edge's identity, upsert the edge with `absorbed=true`, merge
    `prior.status='superseded'` and upsert the `ReingestProgress` marker at `linked`. They land
    together or not at all, so no edge-without-status or edge-without-absorbed window can exist.
    The marker is deleted afterwards by :func:`clear_marker`. Idempotent: a repeat logs nothing.
    Raises `ChangeMonitorStateError`, writing nothing, when the prior or the new node is absent.
    """
    submit_succession_group(
        target,
        target.native_graph,
        step="link",
        new_id=new_id,
        build=lambda: (
            _stamp_superseded_by(prior_id, new_id),
            _upsert_superseded_by(prior_id, new_id, {"absorbed": True}),
            MergeProperty(
                label=_REGULATORY_INSTRUMENT, id=prior_id, properties={"status": "superseded"}
            ),
            UpsertNode(label=_REINGEST_PROGRESS, id=new_id, properties={"stage": LINKED}),
        ),
        missing=f"the native graph has no {prior_id!r} and {new_id!r} pair to link",
    )


def supersede_in_single_tenant(target: SuccessionTarget, prior_id: str, new_id: str) -> None:
    """Write the succession into the merged `policy_system` graph as ONE group (step 2 of 3, D2).

    Issue #213: in order, a defensive `StampEdgeIdentity` (a `policy_system` edge written before the
    gateway has no identity; the gateway drops the stamp when there is none), the
    `SUPERSEDED_BY` `UpsertEdge` and `MergeProperty(prior, status='superseded')`, so the tracked set
    (`status = 'active'`) stops revisiting the prior and `ps-list-ingested` shows `superseded_by`.
    Idempotent. A missing prior or new node means Company Merge did not land it in `policy_system`,
    an inconsistent graph: raises `ChangeMonitorStateError` and writes nothing, leaving the native
    `linked` marker in place so the next sweep retries.
    """
    submit_succession_group(
        target,
        target.single_tenant_graph,
        step="single_tenant_supersede",
        new_id=new_id,
        build=lambda: (
            _stamp_superseded_by(prior_id, new_id),
            _upsert_superseded_by(prior_id, new_id, {}),
            MergeProperty(
                label=_REGULATORY_INSTRUMENT, id=prior_id, properties={"status": "superseded"}
            ),
        ),
        missing=f"policy_system has no {prior_id!r} and {new_id!r} pair to link with SUPERSEDED_BY",
    )
