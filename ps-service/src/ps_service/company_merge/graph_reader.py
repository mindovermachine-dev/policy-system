"""`read_baseline_graph` -- reads a complete `{short}_baseline` graph back
into a `BaselineGraph` (PLAN_REVIEWED.md §4), ready for
`company_merge.dedup`/`company_merge.merge` to consume.

Read-only: every query here is a `MATCH ... RETURN ...`, never a `MERGE`/
`SET`/`DELETE`. Regulation/Role/Requirement/`DEFINES`/`EXPRESSES` are
carried forward unconditionally alongside Obligation/Capability/`HAS`/
`SATISFIED_BY`/`REQUIRES` (§4's rationale: provenance recoverability via
`SATISFIED_BY` -> `EXPRESSES` only holds as a live guarantee if `EXPRESSES`
and the Regulation node it originates from actually exist in whichever
graph a caller traverses).

Node/edge shape cross-checked directly against
`ps_service.domain_mapper.graph_writer`'s actual shipped write path (the
module this reader's queries must round-trip against), not invented by
analogy:

- Regulation: `MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties`, an
  open/variable properties set (title, jurisdiction, effective_date,
  source_type, ...) -- read back the same way
  `ps_service.domain_mapper.extraction._read_regulation_properties` already
  does (`MATCH (r:RegulatoryInstrument) RETURN r`, then `dict(node.properties)`,
  `id` included), via this module's own `_RegulationNode` structural
  Protocol copy.
- Role: properties are `name`/`confidence` only (`RoleNode.properties`).
- Requirement: properties are `text`/`type`/`confidence`/`role_id`
  (`RequirementNode.properties`) -- `role_id` is bookkeeping, not an Edge
  Catalog relationship, carried through unchanged.
- Obligation: properties are `text`/`confidence` only (`ObligationNode.
  properties`) -- no `source_ref` (provenance is transitive).
- Capability: properties are `name`/`confidence` and, when set,
  `description` (`CapabilityNode.properties`) -- `description` is omitted
  from the returned `BaselineNode.properties` dict entirely when absent,
  mirroring how `_upsert_node` never receives a `description` key for a
  Capability minted without one.
- `DEFINES`/`EXPRESSES` edges carry a `source_ref` property
  (`_upsert_regulation_edge`); `HAS`/`SATISFIED_BY`/`REQUIRES` edges carry
  no properties at all (`_upsert_bare_edge`) -- each relationship type here
  is read via its own fixed-literal query, never parsed from a returned
  type string, mirroring `_upsert_regulation_edge`/`_upsert_bare_edge`'s own
  "always a fixed Python literal, never adapter/DB-sourced" design note.

**Deviation from PLAN_REVIEWED.md's "six/seven queries" phrasing**: this
implementation issues ten queries -- one Regulation, one each for
Role/Requirement/Obligation/Capability (four), two provenance-edge queries
(`DEFINES`, `EXPRESSES`) and three bare-edge queries (`HAS`, `SATISFIED_BY`,
`REQUIRES`) -- rather than collapsing the edge reads into one combined
query per category via a runtime `type(e)` dispatch. Each relationship
type's Python-side literal is fixed by which query produced the row, never
parsed/cast from a returned string, matching `graph_writer.py`'s own
"no allow-list needed, always a fixed literal" precedent exactly and
avoiding an unforced runtime-narrowing cast that a combined query would
require. The plan's own count was written as an approximation ("six/seven")
and does not fix a specific number.
"""

from __future__ import annotations

from typing import Protocol, cast

from ps_service.company_merge.falkordb_client import GraphHandle
from ps_service.company_merge.models import (
    BareEdge,
    BaselineGraph,
    BaselineNode,
    ProvenanceEdge,
)

_REGULATION_QUERY = "MATCH (n:RegulatoryInstrument {id: $regulation_id}) RETURN n"
_ROLE_QUERY = "MATCH (n:Role) RETURN n.id, n.name, n.confidence"
_REQUIREMENT_QUERY = "MATCH (n:Requirement) RETURN n.id, n.text, n.type, n.confidence, n.role_id"
_OBLIGATION_QUERY = "MATCH (n:Obligation) RETURN n.id, n.text, n.confidence"
_CAPABILITY_QUERY = "MATCH (n:Capability) RETURN n.id, n.name, n.confidence, n.description"
_DEFINES_QUERY = (
    "MATCH (r:RegulatoryInstrument {id: $regulation_id})-[e:DEFINES]->(n:Role) "
    "RETURN n.id, e.source_ref"
)
_EXPRESSES_QUERY = (
    "MATCH (r:RegulatoryInstrument {id: $regulation_id})-[e:EXPRESSES]->(n:Requirement) "
    "RETURN n.id, e.source_ref"
)
_HAS_QUERY = "MATCH (s:Role)-[:HAS]->(t:Obligation) RETURN s.id, t.id"
_SATISFIED_BY_QUERY = "MATCH (s:Requirement)-[:SATISFIED_BY]->(t:Obligation) RETURN s.id, t.id"
_REQUIRES_QUERY = "MATCH (s:Obligation)-[:REQUIRES]->(t:Capability) RETURN s.id, t.id"


class _RegulationNode(Protocol):
    """Structural stand-in for the `falkordb.Node` `MATCH (n:RegulatoryInstrument
    {id: $regulation_id}) RETURN n` returns -- only `.properties` is ever
    read, mirroring `ps_service.domain_mapper.extraction._RegulationNode`'s
    own minimal structural-Protocol style (own copy, per this component's
    "vendored as an independent copy" convention). A hand-written test fake
    needs only this one attribute to satisfy it."""

    @property
    def properties(self) -> dict[str, object]: ...


def read_baseline_graph(baseline_graph: GraphHandle, regulation_id: str) -> BaselineGraph:
    """Read one regulation's complete `{short}_baseline` graph -- the
    caller has already selected `baseline_graph` (e.g. via
    `ps_service.domain_mapper.falkordb_client.select_graph` +
    `baseline_graph_name(short_name)`); this function does no graph
    selection of its own.

    Every query below is read-only. A baseline graph with zero Obligation/
    Capability nodes (a regulation whose derivation surfaced everything as
    unmatched, per `DeriveObligationsAndCapabilities`'s own AC-004 edge
    case) returns empty tuples for those fields -- and empty tuples for
    every edge collection that would otherwise reference them -- with no
    exception raised.
    """
    regulation_properties = _read_regulation_properties(baseline_graph, regulation_id)
    role_nodes = _read_role_nodes(baseline_graph)
    requirement_nodes = _read_requirement_nodes(baseline_graph)
    obligation_nodes = _read_obligation_nodes(baseline_graph)
    capability_nodes = _read_capability_nodes(baseline_graph)
    provenance_edges = _read_provenance_edges(baseline_graph, regulation_id)
    bare_edges = _read_bare_edges(baseline_graph)

    return BaselineGraph(
        regulation_id=regulation_id,
        regulation_properties=regulation_properties,
        role_nodes=role_nodes,
        requirement_nodes=requirement_nodes,
        obligation_nodes=obligation_nodes,
        capability_nodes=capability_nodes,
        provenance_edges=provenance_edges,
        bare_edges=bare_edges,
    )


def _read_regulation_properties(
    baseline_graph: GraphHandle, regulation_id: str
) -> dict[str, object]:
    """`MATCH (n:RegulatoryInstrument {id: $regulation_id}) RETURN n`, read back as a
    plain properties dict -- mirrors `ps_service.domain_mapper.extraction.
    _read_regulation_properties`'s exact read shape. Absent Regulation node
    (a baseline graph left in an unexpected state) yields an empty dict
    rather than raising -- this function's own contract only covers reading
    whatever is present; whether a missing Regulation node should abort the
    whole merge is `merge.py`'s call, not this reader's.
    """
    result = baseline_graph.query(_REGULATION_QUERY, params={"regulation_id": regulation_id})
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return {}
    node = cast(_RegulationNode, rows[0][0])
    return dict(node.properties)


def _read_role_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    result = baseline_graph.query(_ROLE_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, name, confidence = row
        nodes.append(
            BaselineNode(
                id=cast(str, node_id),
                properties={"name": cast(str, name), "confidence": cast(float, confidence)},
            )
        )
    return tuple(nodes)


def _read_requirement_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    result = baseline_graph.query(_REQUIREMENT_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, text, requirement_type, confidence, role_id = row
        nodes.append(
            BaselineNode(
                id=cast(str, node_id),
                properties={
                    "text": cast(str, text),
                    "type": cast(str, requirement_type),
                    "confidence": cast(float, confidence),
                    "role_id": cast(str, role_id),
                },
            )
        )
    return tuple(nodes)


def _read_obligation_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    result = baseline_graph.query(_OBLIGATION_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, text, confidence = row
        nodes.append(
            BaselineNode(
                id=cast(str, node_id),
                properties={"text": cast(str, text), "confidence": cast(float, confidence)},
            )
        )
    return tuple(nodes)


def _read_capability_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    result = baseline_graph.query(_CAPABILITY_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, name, confidence, description = row
        properties: dict[str, str | float] = {
            "name": cast(str, name),
            "confidence": cast(float, confidence),
        }
        if description is not None:
            properties["description"] = cast(str, description)
        nodes.append(BaselineNode(id=cast(str, node_id), properties=properties))
    return tuple(nodes)


def _read_provenance_edges(
    baseline_graph: GraphHandle, regulation_id: str
) -> tuple[ProvenanceEdge, ...]:
    """`DEFINES` (Regulation -> Role) then `EXPRESSES` (Regulation ->
    Requirement) -- each read via its own fixed-relationship-type query, so
    `relationship_type` is always a Python literal known at the call site,
    never parsed from a returned string (see module docstring)."""
    edges: list[ProvenanceEdge] = []

    defines_result = baseline_graph.query(_DEFINES_QUERY, params={"regulation_id": regulation_id})
    for row in cast("list[list[object]]", defines_result.result_set):
        target_id, source_ref = row
        edges.append(
            ProvenanceEdge(
                relationship_type="DEFINES",
                target_id=cast(str, target_id),
                source_ref=cast(str, source_ref),
            )
        )

    expresses_result = baseline_graph.query(
        _EXPRESSES_QUERY, params={"regulation_id": regulation_id}
    )
    for row in cast("list[list[object]]", expresses_result.result_set):
        target_id, source_ref = row
        edges.append(
            ProvenanceEdge(
                relationship_type="EXPRESSES",
                target_id=cast(str, target_id),
                source_ref=cast(str, source_ref),
            )
        )

    return tuple(edges)


def _read_bare_edges(baseline_graph: GraphHandle) -> tuple[BareEdge, ...]:
    """`HAS` (Role -> Obligation), then `SATISFIED_BY` (Requirement ->
    Obligation), then `REQUIRES` (Obligation -> Capability) -- each read via
    its own fixed-relationship-type query, same reasoning as
    `_read_provenance_edges`. Endpoint ids here are BASELINE-LOCAL; rewiring
    a `REQUIRES` edge's Capability endpoint onto its canonical id is
    `dedup`/`graph_writer`'s job, not this reader's (§6). Obligation, Role,
    and Requirement endpoints are passthrough (#42) -- their baseline-local
    id is already final."""
    edges: list[BareEdge] = []

    has_result = baseline_graph.query(_HAS_QUERY)
    for row in cast("list[list[object]]", has_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="HAS",
                source_id=cast(str, source_id),
                target_id=cast(str, target_id),
            )
        )

    satisfied_by_result = baseline_graph.query(_SATISFIED_BY_QUERY)
    for row in cast("list[list[object]]", satisfied_by_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="SATISFIED_BY",
                source_id=cast(str, source_id),
                target_id=cast(str, target_id),
            )
        )

    requires_result = baseline_graph.query(_REQUIRES_QUERY)
    for row in cast("list[list[object]]", requires_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="REQUIRES",
                source_id=cast(str, source_id),
                target_id=cast(str, target_id),
            )
        )

    return tuple(edges)
