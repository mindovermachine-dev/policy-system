"""Baseline-graph writes of `ps_service.domain_mapper`, through the Graph Write Gateway (#209).

The Domain Mapper issues no write Cypher. Each stage turns its in-memory result into gateway
primitives and submits them as ONE group on the `{short}_baseline` graph, under the run's audit
event, through a `GroupSubmitter`:

- `persist_role_and_requirement_graph` (extraction): validates every Requirement's `role_id`
  against the Role nodes of the same call (`_validate_role_references`, B3 fix (a): a dangling
  reference raises before anything is built, stamped or submitted), builds the instrument, Role,
  `DEFINES`, Requirement and `EXPRESSES` primitives (`build_role_and_requirement_primitives`),
  and submits them. Every edge endpoint is in the group, so nothing is read.
- `persist_obligation_and_capability_graph` (derivation): builds the Obligation and Capability
  nodes and the `HAS`, `SATISFIED_BY` and `REQUIRES` edges
  (`build_obligation_and_capability_primitives`). The `HAS` and `SATISFIED_BY` edges join a Role
  and a Requirement that a prior extraction wrote, outside this group, so the one read this
  module makes is the batched existing-node read (`_read_existing_ids`) through the read-only
  `{short}_baseline` handle; an edge whose endpoint exists nowhere is dropped silently
  (`_drop_edges_with_missing_endpoints`, AC-BI-004, as the old `MATCH ... MERGE` wrote nothing).
  Nothing to write submits nothing (a group needs at least one primitive).

Shared sequence (`_submit_baseline_group`): the graph's legacy edges are stamped with their
identity before the first group, the group is submitted (`submit_or_raise`), and the graph must be
caught up before the stage returns (`require_caught_up`), because the next stage reads it.
Derivation additionally requires the graph caught up BEFORE its endpoint read, so a group still
waiting to be applied cannot make an edge look dangling and be dropped.

Builders de-duplicate before submitting, as the old `MERGE` folded repeats: one node primitive
per (label, id) with later properties overriding earlier ones, one edge per identity with the
last occurrence winning. Edge identities are `{type}:{source_id}->{target_id}` (`_edge_identity`),
the scheme of the internal-seed path and of the edge-identity backfill. Every value (`source_ref`,
`role_id`, ...) travels as a primitive property; nothing is interpolated into a query. A property
the gateway cannot store raises `DomainMapperPersistenceError` before anything is submitted.

Failures reach callers as `DomainMapperPersistenceError("FalkorDB write failed: ...")`, whether
the read failed, the gateway refused the group, or the group was committed but not applied;
write-side dependency health is the gateway's, read-side health is recorded here.

**Why derivation has no validate-then-write pass.** Unlike a Requirement's `role_id` (set by a
different stage than the one building `role_nodes`, the decoupling behind B3), the Obligation
and Capability endpoints of the derivation edges are self-consistent by construction within
`derivation.py`: an edge collection is only ever appended to alongside its node. The Role and
Requirement endpoints belong to a prior extraction and are checked on the READ side
(`derivation._read_requirements_by_role`, B3 fix (b)) before any LLM call, and again against the
graph by the existing-node read here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import redis.exceptions
from pydantic import ValidationError

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.domain_mapper.errors import DomainMapperPersistenceError
from ps_service.graph_gateway.models import MutationGroup, NodeRef, UpsertEdge, UpsertNode
from ps_service.ingestion.graph_submission import (
    require_caught_up,
    stamp_legacy_edges_before_first_write,
    submit_or_raise,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.domain_mapper.models import (
        CapabilityNode,
        CapabilityRequiresEdge,
        ObligationHasEdge,
        ObligationNode,
        RequirementExpressesEdge,
        RequirementNode,
        RequirementSatisfiedByEdge,
        RoleDefinesEdge,
        RoleNode,
    )
    from ps_service.graph_gateway.models import Primitive
    from ps_service.ingestion.graph_submission import GroupSubmitter

_REGULATORY_INSTRUMENT_LABEL = "RegulatoryInstrument"
_ROLE_LABEL = "Role"
_REQUIREMENT_LABEL = "Requirement"
_OBLIGATION_LABEL = "Obligation"
_CAPABILITY_LABEL = "Capability"


def _validate_role_references(
    role_nodes: tuple[RoleNode, ...], requirement_nodes: tuple[RequirementNode, ...]
) -> None:
    """B3 fix (a): a whole-collection validation pass, called once before any write.

    Runs before `persist_role_and_requirement_graph` builds or submits anything for ANY
    element — not interleaved validate-then-write per element.

    Checks every Requirement node's `role_id` property against the set of
    Role node ids ALSO about to be persisted in this same call. Raises
    `DomainMapperPersistenceError` on the first violation found, with nothing submitted by
    the time this raises.
    """
    role_ids = {node.id for node in role_nodes}
    for requirement in requirement_nodes:
        dangling_role_id = requirement.properties.get("role_id")
        if dangling_role_id not in role_ids:
            raise DomainMapperPersistenceError(
                f"Requirement {requirement.id!r} references role_id "
                f"{dangling_role_id!r}, which is not among the Role nodes "
                "being persisted in this call"
            )


# --- Graph Write Gateway builders (issue #209) ---


def _edge_identity(edge_type: str, source_id: str, target_id: str) -> str:
    """`{type}:{source}->{target}`: the gateway identity of an edge, one per (type, endpoints).

    The scheme of `ingestion.adapters.internal_seed.persist._edge_identity` and of the edge
    identity backfill, so one instrument yields the same edges whichever path ingested it.
    Same-type edges between the same two nodes share an identity, as `MERGE (a)-[:T]->(b)`
    collapsed them.
    """
    return f"{edge_type}:{source_id}->{target_id}"


def _unstorable(what: str) -> DomainMapperPersistenceError:
    return DomainMapperPersistenceError(
        f"{what} has a property the graph cannot store "
        "(a reserved key, or a value that is not finite or is out of range)"
    )


def _merged_nodes(
    specs: Iterable[tuple[str, str, Mapping[str, object]]],
) -> tuple[UpsertNode, ...]:
    """One node primitive per `(label, id)`, in first-seen order, later properties winning.

    The legacy writer issued `MERGE (n:L {id}) SET n += props` per node, so a repeated id was
    one node holding the properties of every occurrence, the last overriding the others. The
    gateway applies a group's primitives in sequence the same way, but its digest and log would
    show the node twice; folding here keeps one primitive per node.

    Raises:
        DomainMapperPersistenceError: a node carries a property the gateway cannot store (a
            reserved key such as `id`, a non-finite number); the legacy `SET n += $properties`
            accepted them silently.
    """
    folded: dict[tuple[str, str], dict[str, object]] = {}
    for label, node_id, properties in specs:
        folded.setdefault((label, node_id), {}).update(properties)
    nodes: list[UpsertNode] = []
    for (label, node_id), properties in folded.items():
        try:
            nodes.append(UpsertNode(label=label, id=node_id, properties=properties))
        except ValidationError as exc:
            raise _unstorable(f"{label} {node_id!r}") from exc
    return tuple(nodes)


def _edge(
    edge_type: str,
    source: tuple[str, str],
    target: tuple[str, str],
    properties: dict[str, object],
) -> UpsertEdge:
    try:
        return UpsertEdge(
            type=edge_type,
            identity=_edge_identity(edge_type, source[1], target[1]),
            source=NodeRef(label=source[0], id=source[1]),
            target=NodeRef(label=target[0], id=target[1]),
            properties=properties,
        )
    except ValidationError as exc:
        raise _unstorable(f"{edge_type} edge {source[1]!r} -> {target[1]!r}") from exc


def _merged_edges(edges: Iterable[UpsertEdge]) -> tuple[UpsertEdge, ...]:
    """One edge primitive per identity, in first-seen order, the last occurrence winning.

    As the legacy `MERGE (a)-[e:T]->(b) SET e.source_ref = ...` collapsed a repeated edge to
    one whose properties came from the later statement.
    """
    by_identity: dict[str, UpsertEdge] = {}
    for edge in edges:
        by_identity[edge.identity] = edge
    return tuple(by_identity.values())


def build_role_and_requirement_primitives(
    regulatory_instrument_id: str,
    regulatory_instrument_properties: dict[str, object],
    role_nodes: tuple[RoleNode, ...],
    role_edges: tuple[RoleDefinesEdge, ...],
    requirement_nodes: tuple[RequirementNode, ...],
    requirement_edges: tuple[RequirementExpressesEdge, ...],
) -> tuple[Primitive, ...]:
    """The extraction group's mutations: instrument, Roles, DEFINES, Requirements, EXPRESSES.

    Pure: nothing is read or submitted. `id` is stripped from the instrument's property bag (a
    whole-bag copy of the NATIVE node, whose `id` may differ in casing and must never rewrite
    the match key, BASELINE.md row #2). Every edge carries `source_ref` and the identity
    `{type}:{instrument}->{target}`; both endpoints of every edge are in the group.

    Raises:
        DomainMapperPersistenceError: a node or edge carries a property the gateway cannot
            store; nothing has been submitted by then.
    """
    instrument = _merged_nodes(
        [
            (
                _REGULATORY_INSTRUMENT_LABEL,
                regulatory_instrument_id,
                {k: v for k, v in regulatory_instrument_properties.items() if k != "id"},
            )
        ]
    )
    source = (_REGULATORY_INSTRUMENT_LABEL, regulatory_instrument_id)
    roles = _merged_nodes((_ROLE_LABEL, node.id, node.properties) for node in role_nodes)
    defines = _merged_edges(
        _edge("DEFINES", source, (_ROLE_LABEL, edge.role_node_id), {"source_ref": edge.source_ref})
        for edge in role_edges
    )
    requirements = _merged_nodes(
        (_REQUIREMENT_LABEL, node.id, node.properties) for node in requirement_nodes
    )
    expresses = _merged_edges(
        _edge(
            "EXPRESSES",
            source,
            (_REQUIREMENT_LABEL, edge.requirement_node_id),
            {"source_ref": edge.source_ref},
        )
        for edge in requirement_edges
    )
    return (*instrument, *roles, *defines, *requirements, *expresses)


def _submit_baseline_group(
    submitter: GroupSubmitter,
    baseline_graph_name: str,
    audit_event_id: str,
    primitives: tuple[Primitive, ...],
    *,
    checkpoint_requested: bool = False,
) -> None:
    """Stamp the graph's legacy edges, submit `primitives` as one group, require it applied.

    `checkpoint_requested` asks the gateway to record the graph's digest once the group is
    applied; only the derivation group, the last one the Domain Mapper writes, sets it.

    Stamping runs after the caller's pre-flight validation and building (a rejected request
    writes and logs nothing) and before the first group. `require_caught_up` is mandatory: the
    next stage reads this graph.

    Raises:
        DomainMapperPersistenceError: `FalkorDB write failed: <sanitized text>` if the gateway
            refused the group, could not reach its stores, or the group was committed but not
            applied.
    """
    group = MutationGroup(
        graph=baseline_graph_name,
        audit_event_id=audit_event_id,
        primitives=primitives,
        checkpoint_requested=checkpoint_requested,
    )
    stamp_legacy_edges_before_first_write(
        submitter, baseline_graph_name, audit_event_id, DomainMapperPersistenceError
    )
    submit_or_raise(submitter, group, DomainMapperPersistenceError)
    require_caught_up(submitter, baseline_graph_name, DomainMapperPersistenceError)


def persist_role_and_requirement_graph(  # noqa: PLR0913 -- the gateway triple plus the instrument and the four collections
    submitter: GroupSubmitter,
    baseline_graph_name: str,
    audit_event_id: str,
    *,
    regulatory_instrument_id: str,
    regulatory_instrument_properties: dict[str, object],
    role_nodes: tuple[RoleNode, ...],
    role_edges: tuple[RoleDefinesEdge, ...],
    requirement_nodes: tuple[RequirementNode, ...],
    requirement_edges: tuple[RequirementExpressesEdge, ...],
) -> None:
    """Submit one extraction run's graph as ONE gateway group on `baseline_graph_name`.

    Sequence: `_validate_role_references` (a dangling `role_id` raises before anything is built),
    build the primitives (an unstorable property raises before anything is submitted), stamp the
    graph's legacy edges, submit, require the graph caught up. The group is atomic, so a failure
    leaves none of this run's content behind. No LLM is called here: every id and property was
    computed by the caller.

    Raises:
        DomainMapperPersistenceError: a Requirement's `role_id` is not among `role_nodes`, a
            property cannot be stored, or the gateway did not take the group.
    """
    _validate_role_references(role_nodes, requirement_nodes)
    primitives = build_role_and_requirement_primitives(
        regulatory_instrument_id,
        regulatory_instrument_properties,
        role_nodes,
        role_edges,
        requirement_nodes,
        requirement_edges,
    )
    _submit_baseline_group(submitter, baseline_graph_name, audit_event_id, primitives)


def _read_existing_ids(graph: GraphHandle, label: str, node_ids: Iterable[str]) -> set[str]:
    """Which of `node_ids` are `label` nodes of `graph`: one batched read, ids via `params`.

    `label` is always one of this module's fixed label literals, never adapter- or LLM-sourced,
    so interpolating it is safe; the ids travel as a parameter. FalkorDB reachability is
    recorded in `ps_service.dependency_health` as the old direct writes recorded it.

    Raises:
        DomainMapperPersistenceError: `FalkorDB write failed: <error>` if the read could not be
            made; nothing has been submitted by then.
    """
    try:
        result = graph.query(
            f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id", params={"ids": sorted(node_ids)}
        )
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise DomainMapperPersistenceError(f"FalkorDB write failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return {str(row[0]) for row in cast("list[list[object]]", result.result_set)}


def _drop_edges_with_missing_endpoints(
    graph: GraphHandle, nodes: tuple[UpsertNode, ...], edges: tuple[UpsertEdge, ...]
) -> tuple[UpsertEdge, ...]:
    """Keep the edges whose both endpoints exist: in `nodes` (this group) or in `graph`.

    The legacy `MATCH ... MERGE` wrote nothing for an edge with a missing endpoint, without error
    or log entry (AC-BI-004); a group edge with a missing endpoint would instead fail the whole
    group, so such edges are dropped here, silently. Endpoints outside the group are resolved with
    one batched read per label (none if every endpoint is in the group).
    """
    in_group = {(node.label, node.id) for node in nodes}
    outside: dict[str, set[str]] = {}
    for edge in edges:
        for ref in (edge.source, edge.target):
            if (ref.label, ref.id) not in in_group:
                outside.setdefault(ref.label, set()).add(ref.id)
    present = set(in_group)
    for label, node_ids in outside.items():
        present |= {(label, found) for found in _read_existing_ids(graph, label, node_ids)}
    return tuple(
        edge
        for edge in edges
        if (edge.source.label, edge.source.id) in present
        and (edge.target.label, edge.target.id) in present
    )


def build_obligation_and_capability_primitives(
    obligation_nodes: tuple[ObligationNode, ...],
    capability_nodes: tuple[CapabilityNode, ...],
    has_edges: tuple[ObligationHasEdge, ...],
    satisfied_by_edges: tuple[RequirementSatisfiedByEdge, ...],
    requires_edges: tuple[CapabilityRequiresEdge, ...],
) -> tuple[tuple[UpsertNode, ...], tuple[UpsertEdge, ...]]:
    """The derivation group's mutations: `(nodes, edges)`, each in write order.

    Nodes: Obligations, then Capabilities. Edges: `HAS` (Role to Obligation), `SATISFIED_BY`
    (Requirement to Obligation), `REQUIRES` (Obligation to Capability). The three edge types
    carry no properties (Edge Catalog). Pure: nothing is read or submitted.

    Raises:
        DomainMapperPersistenceError: a node carries a property the gateway cannot store.
    """
    nodes = _merged_nodes(
        [
            *((_OBLIGATION_LABEL, node.id, node.properties) for node in obligation_nodes),
            *((_CAPABILITY_LABEL, node.id, node.properties) for node in capability_nodes),
        ]
    )
    edges = _merged_edges(
        [
            *(
                _edge(
                    "HAS",
                    (_ROLE_LABEL, edge.role_node_id),
                    (_OBLIGATION_LABEL, edge.obligation_node_id),
                    {},
                )
                for edge in has_edges
            ),
            *(
                _edge(
                    "SATISFIED_BY",
                    (_REQUIREMENT_LABEL, edge.requirement_id),
                    (_OBLIGATION_LABEL, edge.obligation_node_id),
                    {},
                )
                for edge in satisfied_by_edges
            ),
            *(
                _edge(
                    "REQUIRES",
                    (_OBLIGATION_LABEL, edge.obligation_node_id),
                    (_CAPABILITY_LABEL, edge.capability_node_id),
                    {},
                )
                for edge in requires_edges
            ),
        ]
    )
    return nodes, edges


def persist_obligation_and_capability_graph(  # noqa: PLR0913 -- the gateway triple, the read handle and the five collections
    submitter: GroupSubmitter,
    baseline_graph_name: str,
    audit_event_id: str,
    *,
    graph: GraphHandle,
    obligation_nodes: tuple[ObligationNode, ...],
    has_edges: tuple[ObligationHasEdge, ...],
    satisfied_by_edges: tuple[RequirementSatisfiedByEdge, ...],
    capability_nodes: tuple[CapabilityNode, ...],
    requires_edges: tuple[CapabilityRequiresEdge, ...],
) -> None:
    """Submit one derivation run's graph as ONE gateway group on `baseline_graph_name`.

    `graph` is the read-only `{short}_baseline` handle: it is only asked which `Role` and
    `Requirement` endpoints exist. Sequence: build (an unstorable property raises before
    anything else happens), require the graph caught up with its log (so an earlier group still
    waiting to be applied cannot make an edge look dangling and be dropped, AC-BI-004), read the
    endpoints outside the group, drop edges whose endpoint exists nowhere (no error, no log
    entry), stamp the graph's legacy edges, submit, require caught up again (the merge stage
    reads this graph next). Nothing to write (all five collections empty) returns at once, with
    no stamp, read or group. No LLM is called here: every id was computed by the caller.

    Raises:
        DomainMapperPersistenceError: a property cannot be stored, the graph is behind its log,
            the read failed, or the gateway did not take the group.
    """
    nodes, edges = build_obligation_and_capability_primitives(
        obligation_nodes, capability_nodes, has_edges, satisfied_by_edges, requires_edges
    )
    if not nodes and not edges:
        return
    require_caught_up(submitter, baseline_graph_name, DomainMapperPersistenceError)
    kept_edges = _drop_edges_with_missing_endpoints(graph, nodes, edges)
    _submit_baseline_group(
        submitter,
        baseline_graph_name,
        audit_event_id,
        (*nodes, *kept_edges),
        checkpoint_requested=True,
    )
