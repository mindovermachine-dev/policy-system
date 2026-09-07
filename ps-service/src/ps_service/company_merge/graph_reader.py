"""`read_baseline_graph` -- read a complete `{short}_baseline` graph into a `BaselineGraph`.

Ready for `company_merge.dedup`/`company_merge.merge` to consume
(PLAN_REVIEWED.md §4).

Read-only: every query here is a `MATCH ... RETURN ...`, never a `MERGE`/
`SET`/`DELETE`. RegulatoryInstrument/Role/Requirement/`DEFINES`/`EXPRESSES` are
carried forward unconditionally alongside Obligation/Capability/`HAS`/
`SATISFIED_BY`/`REQUIRES` (§4's rationale: provenance recoverability via
`SATISFIED_BY` -> `EXPRESSES` only holds as a live guarantee if `EXPRESSES`
and the RegulatoryInstrument node it originates from actually exist in whichever
graph a caller traverses).

Node/edge shape cross-checked directly against
`ps_service.domain_mapper.graph_writer`'s actual shipped write path (the
module this reader's queries must round-trip against), not invented by
analogy:

- RegulatoryInstrument: `MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties`, an
  open/variable properties set (title, jurisdiction, effective_date,
  source_type, ...) -- read back the same way
  `ps_service.domain_mapper.extraction._read_regulatory_instrument_properties` already
  does (`MATCH (r:RegulatoryInstrument) RETURN r`, then `dict(node.properties)`,
  `id` included), via this module's own `_RegulatoryInstrumentNode` structural
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
  (`_upsert_regulatory_instrument_edge`); `HAS`/`SATISFIED_BY`/`REQUIRES` edges carry
  no properties at all (`_upsert_bare_edge`) -- each relationship type here
  is read via its own fixed-literal query, never parsed from a returned
  type string, mirroring `_upsert_regulatory_instrument_edge`/`_upsert_bare_edge`'s own
  "always a fixed Python literal, never adapter/DB-sourced" design note.

**Deviation from PLAN_REVIEWED.md's "six/seven queries" phrasing**: this
implementation issues sixteen queries -- one RegulatoryInstrument, one each
for Role/Requirement/Obligation/Capability/Policy/Standard/Control (seven),
two provenance-edge queries (`DEFINES`, `EXPRESSES`) and six bare-edge
queries (`HAS`, `SATISFIED_BY`, `REQUIRES`, `GOVERNED_BY`, `SUPPORTED_BY`,
`IMPLEMENTED_BY`, the last three added by issue #54's S4) -- rather than
collapsing the edge reads into one combined query per category via a
runtime `type(e)` dispatch. Each relationship type's Python-side literal is
fixed by which query produced the row, never parsed/cast from a returned
string, matching `graph_writer.py`'s own "no allow-list needed, always a
fixed literal" precedent exactly and avoiding an unforced runtime-narrowing
cast that a combined query would require. The plan's own count was written
as an approximation ("six/seven") and does not fix a specific number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from ps_service.company_merge.models import (
    BareEdge,
    BaselineGraph,
    BaselineNode,
    ProvenanceEdge,
)

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle

_REGULATORY_INSTRUMENT_QUERY = (
    "MATCH (n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n"
)
_ROLE_QUERY = "MATCH (n:Role) RETURN n.id, n.name, n.confidence"
_REQUIREMENT_QUERY = "MATCH (n:Requirement) RETURN n.id, n.text, n.type, n.confidence, n.role_id"
_OBLIGATION_QUERY = "MATCH (n:Obligation) RETURN n.id, n.text, n.confidence"
_CAPABILITY_QUERY = "MATCH (n:Capability) RETURN n.id, n.name, n.confidence, n.description"
_DEFINES_QUERY = (
    "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id})-[e:DEFINES]->(n:Role) "
    "RETURN n.id, e.source_ref"
)
_EXPRESSES_QUERY = (
    "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id})-[e:EXPRESSES]->(n:Requirement) "
    "RETURN n.id, e.source_ref"
)
_HAS_QUERY = "MATCH (s:Role)-[:HAS]->(t:Obligation) RETURN s.id, t.id"
_SATISFIED_BY_QUERY = "MATCH (s:Requirement)-[:SATISFIED_BY]->(t:Obligation) RETURN s.id, t.id"
_REQUIRES_QUERY = "MATCH (s:Obligation)-[:REQUIRES]->(t:Capability) RETURN s.id, t.id"

# issue #54, S4 -- Policy/Standard/Control + governance edges. Empty result
# sets for an external-sourced baseline (DeriveGovernanceArtifacts never ran).
_POLICY_QUERY = "MATCH (n:Policy) RETURN n.id, n.title, n.status, n.confidence"
_STANDARD_QUERY = (
    "MATCH (n:Standard) RETURN n.id, n.title, n.implementation_status, n.confidence, n.description"
)
_CONTROL_QUERY = (
    "MATCH (n:Control) RETURN n.id, n.type, n.title, n.implementation_status, "
    "n.confidence, n.description"
)
_GOVERNED_BY_QUERY = "MATCH (s:Capability)-[:GOVERNED_BY]->(t:Policy) RETURN s.id, t.id"
_SUPPORTED_BY_QUERY = "MATCH (s:Policy)-[:SUPPORTED_BY]->(t:Standard) RETURN s.id, t.id"
_IMPLEMENTED_BY_QUERY = "MATCH (s:Standard)-[:IMPLEMENTED_BY]->(t:Control) RETURN s.id, t.id"


class _RegulatoryInstrumentNode(Protocol):
    """Structural stand-in for the `falkordb.Node` a `RETURN n` on RegulatoryInstrument returns.

    Only `.properties` is ever read, mirroring
    `ps_service.domain_mapper.extraction._RegulatoryInstrumentNode`'s own
    minimal structural-Protocol style (own copy, per this component's
    "vendored as an independent copy" convention). A hand-written test fake
    needs only this one attribute to satisfy it.
    """

    @property
    def properties(self) -> dict[str, object]: ...


def read_baseline_graph(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> BaselineGraph:
    """Read one regulation's complete `{short}_baseline` graph.

    The caller has already selected `baseline_graph` (e.g. via
    `ps_service.domain_mapper.falkordb_client.select_graph` +
    `baseline_graph_name(short_name)`); this function does no graph selection
    of its own.

    Every query below is read-only. A baseline graph with zero Obligation/
    Capability nodes (a regulation whose derivation surfaced everything as
    unmatched, per `DeriveObligationsAndCapabilities`'s own AC-004 edge
    case) returns empty tuples for those fields -- and empty tuples for
    every edge collection that would otherwise reference them -- with no
    exception raised.
    """
    regulatory_instrument_properties = _read_regulatory_instrument_properties(
        baseline_graph, regulatory_instrument_id
    )
    role_nodes = _read_role_nodes(baseline_graph)
    requirement_nodes = _read_requirement_nodes(baseline_graph)
    obligation_nodes = _read_obligation_nodes(baseline_graph)
    capability_nodes = _read_capability_nodes(baseline_graph)
    provenance_edges = _read_provenance_edges(baseline_graph, regulatory_instrument_id)
    bare_edges = _read_bare_edges(baseline_graph)
    policy_nodes = _read_policy_nodes(baseline_graph)
    standard_nodes = _read_standard_nodes(baseline_graph)
    control_nodes = _read_control_nodes(baseline_graph)
    governance_edges = _read_governance_edges(baseline_graph)

    return BaselineGraph(
        regulatory_instrument_id=regulatory_instrument_id,
        regulatory_instrument_properties=regulatory_instrument_properties,
        role_nodes=role_nodes,
        requirement_nodes=requirement_nodes,
        obligation_nodes=obligation_nodes,
        capability_nodes=capability_nodes,
        provenance_edges=provenance_edges,
        bare_edges=bare_edges,
        policy_nodes=policy_nodes,
        standard_nodes=standard_nodes,
        control_nodes=control_nodes,
        governance_edges=governance_edges,
    )


def _read_regulatory_instrument_properties(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> dict[str, object]:
    """Read the RegulatoryInstrument node back as a plain properties dict.

    `MATCH (n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n`,
    mirroring
    `ps_service.domain_mapper.extraction._read_regulatory_instrument_properties`'s
    exact read shape. An absent RegulatoryInstrument node (a baseline graph
    left in an unexpected state) yields an empty dict rather than raising --
    this function's own contract only covers reading whatever is present;
    whether a missing RegulatoryInstrument node should abort the whole merge
    is `merge.py`'s call, not this reader's.
    """
    result = baseline_graph.query(
        _REGULATORY_INSTRUMENT_QUERY, params={"regulatory_instrument_id": regulatory_instrument_id}
    )
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return {}
    node = cast("_RegulatoryInstrumentNode", rows[0][0])
    return dict(node.properties)


def _read_role_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    result = baseline_graph.query(_ROLE_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, name, confidence = row
        nodes.append(
            BaselineNode(
                id=cast("str", node_id),
                properties={"name": cast("str", name), "confidence": cast("float", confidence)},
            )
        )
    return tuple(nodes)


def _read_requirement_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    """Read every Requirement node, omitting `role_id` when the graph returns `NULL`.

    F1 (CHANGES.md): `role_id` is bookkeeping, not always present -- an
    internal-source Requirement can lack one. D6's "never write `None`" rule
    applied symmetrically on the read side: `role_id` is added to
    `properties` only `if role_id is not None`, never unconditionally
    `cast()`.
    """
    result = baseline_graph.query(_REQUIREMENT_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, text, requirement_type, confidence, role_id = row
        properties: dict[str, str | float] = {
            "text": cast("str", text),
            "type": cast("str", requirement_type),
            "confidence": cast("float", confidence),
        }
        if role_id is not None:
            properties["role_id"] = cast("str", role_id)
        nodes.append(BaselineNode(id=cast("str", node_id), properties=properties))
    return tuple(nodes)


def _read_obligation_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    result = baseline_graph.query(_OBLIGATION_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, text, confidence = row
        nodes.append(
            BaselineNode(
                id=cast("str", node_id),
                properties={"text": cast("str", text), "confidence": cast("float", confidence)},
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
            "name": cast("str", name),
            "confidence": cast("float", confidence),
        }
        if description is not None:
            properties["description"] = cast("str", description)
        nodes.append(BaselineNode(id=cast("str", node_id), properties=properties))
    return tuple(nodes)


def _read_provenance_edges(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> tuple[ProvenanceEdge, ...]:
    """Read the `DEFINES` then `EXPRESSES` provenance edges from the RegulatoryInstrument.

    `DEFINES` targets Role, `EXPRESSES` targets Requirement. Each is read via
    its own fixed-relationship-type query, so `relationship_type` is always a
    Python literal known at the call site, never parsed from a returned
    string (see module docstring).
    """
    edges: list[ProvenanceEdge] = []

    defines_result = baseline_graph.query(
        _DEFINES_QUERY, params={"regulatory_instrument_id": regulatory_instrument_id}
    )
    for row in cast("list[list[object]]", defines_result.result_set):
        target_id, source_ref = row
        edges.append(
            ProvenanceEdge(
                relationship_type="DEFINES",
                target_id=cast("str", target_id),
                source_ref=cast("str", source_ref),
            )
        )

    expresses_result = baseline_graph.query(
        _EXPRESSES_QUERY, params={"regulatory_instrument_id": regulatory_instrument_id}
    )
    for row in cast("list[list[object]]", expresses_result.result_set):
        target_id, source_ref = row
        edges.append(
            ProvenanceEdge(
                relationship_type="EXPRESSES",
                target_id=cast("str", target_id),
                source_ref=cast("str", source_ref),
            )
        )

    return tuple(edges)


def _read_bare_edges(baseline_graph: GraphHandle) -> tuple[BareEdge, ...]:
    """Read the `HAS`, `SATISFIED_BY`, and `REQUIRES` bare edges.

    `HAS` (Role -> Obligation), then `SATISFIED_BY` (Requirement ->
    Obligation), then `REQUIRES` (Obligation -> Capability) -- each read via
    its own fixed-relationship-type query, same reasoning as
    `_read_provenance_edges`. Endpoint ids here are BASELINE-LOCAL; rewiring
    a `REQUIRES` edge's Capability endpoint onto its canonical id is
    `dedup`/`graph_writer`'s job, not this reader's (§6). Obligation, Role,
    and Requirement endpoints are passthrough (#42) -- their baseline-local
    id is already final.
    """
    edges: list[BareEdge] = []

    has_result = baseline_graph.query(_HAS_QUERY)
    for row in cast("list[list[object]]", has_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="HAS",
                source_id=cast("str", source_id),
                target_id=cast("str", target_id),
            )
        )

    satisfied_by_result = baseline_graph.query(_SATISFIED_BY_QUERY)
    for row in cast("list[list[object]]", satisfied_by_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="SATISFIED_BY",
                source_id=cast("str", source_id),
                target_id=cast("str", target_id),
            )
        )

    requires_result = baseline_graph.query(_REQUIRES_QUERY)
    for row in cast("list[list[object]]", requires_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="REQUIRES",
                source_id=cast("str", source_id),
                target_id=cast("str", target_id),
            )
        )

    return tuple(edges)


def _read_policy_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    """Read every Policy node (issue #54, S4). Empty for an external-sourced baseline.

    Properties are `title`/`status`/`confidence` -- never optional, mirroring
    `_read_role_nodes`'s own "no optional fields" shape (`DeriveGovernanceArtifacts`
    always sets all three at mint time).
    """
    result = baseline_graph.query(_POLICY_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, title, status, confidence = row
        nodes.append(
            BaselineNode(
                id=cast("str", node_id),
                properties={
                    "title": cast("str", title),
                    "status": cast("str", status),
                    "confidence": cast("float", confidence),
                },
            )
        )
    return tuple(nodes)


def _read_standard_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    """Read every Standard node (issue #54, S4). Empty for an external-sourced baseline.

    Properties are `title`/`implementation_status`/`confidence` and, when
    set, `description` -- mirroring `_read_capability_nodes`'s own
    "optional description" shape.
    """
    result = baseline_graph.query(_STANDARD_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, title, implementation_status, confidence, description = row
        properties: dict[str, str | float] = {
            "title": cast("str", title),
            "implementation_status": cast("str", implementation_status),
            "confidence": cast("float", confidence),
        }
        if description is not None:
            properties["description"] = cast("str", description)
        nodes.append(BaselineNode(id=cast("str", node_id), properties=properties))
    return tuple(nodes)


def _read_control_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    """Read every Control node (issue #54, S4). Empty for an external-sourced baseline.

    Properties are `type`/`title`/`implementation_status`/`confidence` and,
    when set, `description` -- mirroring `_read_capability_nodes`'s own
    "optional description" shape. The four AC-BI-017 operational fields
    (`execution_frequency`/`last_test_date`/`next_review_date`/
    `evidence_ref`) are never written at mint time, so they are never present
    to read back here either -- no special handling needed.
    """
    result = baseline_graph.query(_CONTROL_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, control_type, title, implementation_status, confidence, description = row
        properties: dict[str, str | float] = {
            "type": cast("str", control_type),
            "title": cast("str", title),
            "implementation_status": cast("str", implementation_status),
            "confidence": cast("float", confidence),
        }
        if description is not None:
            properties["description"] = cast("str", description)
        nodes.append(BaselineNode(id=cast("str", node_id), properties=properties))
    return tuple(nodes)


def _read_governance_edges(baseline_graph: GraphHandle) -> tuple[BareEdge, ...]:
    """Read the `GOVERNED_BY`, `SUPPORTED_BY`, and `IMPLEMENTED_BY` edges (issue #54, S4).

    `GOVERNED_BY` (Capability -> Policy), then `SUPPORTED_BY` (Policy ->
    Standard), then `IMPLEMENTED_BY` (Standard -> Control) -- each read via
    its own fixed-relationship-type query, same reasoning as
    `_read_bare_edges`. Endpoint ids here are BASELINE-LOCAL; rewiring a
    `GOVERNED_BY` edge's Policy endpoint onto its canonical id is
    `dedup`/`graph_writer`'s job, not this reader's. Empty for an
    external-sourced baseline, with no exception raised.
    """
    edges: list[BareEdge] = []

    governed_by_result = baseline_graph.query(_GOVERNED_BY_QUERY)
    for row in cast("list[list[object]]", governed_by_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="GOVERNED_BY",
                source_id=cast("str", source_id),
                target_id=cast("str", target_id),
            )
        )

    supported_by_result = baseline_graph.query(_SUPPORTED_BY_QUERY)
    for row in cast("list[list[object]]", supported_by_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="SUPPORTED_BY",
                source_id=cast("str", source_id),
                target_id=cast("str", target_id),
            )
        )

    implemented_by_result = baseline_graph.query(_IMPLEMENTED_BY_QUERY)
    for row in cast("list[list[object]]", implemented_by_result.result_set):
        source_id, target_id = row
        edges.append(
            BareEdge(
                relationship_type="IMPLEMENTED_BY",
                source_id=cast("str", source_id),
                target_id=cast("str", target_id),
            )
        )

    return tuple(edges)
