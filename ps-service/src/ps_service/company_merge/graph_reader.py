"""`read_baseline_graph` -- read a complete `{short}_baseline` graph into a `BaselineGraph`.

The result is what `company_merge.dedup` and `company_merge.merge` consume.

Read-only: every query here is a `MATCH ... RETURN ...`, never a `MERGE`/`SET`/`DELETE`.
RegulatoryInstrument/Role/Requirement/`DEFINES`/`EXPRESSES` are carried forward unconditionally
alongside Obligation/Capability/`HAS`/`SATISFIED_BY`/`REQUIRES`: provenance recoverability via
`SATISFIED_BY` -> `EXPRESSES` only holds if `EXPRESSES` and the RegulatoryInstrument node it
originates from exist in whichever graph a caller traverses.

Node and edge shapes match what `ps_service.domain_mapper.graph_writer` writes, which these
queries must round-trip against:

- RegulatoryInstrument: an open, variable properties set (title, jurisdiction, effective_date,
  source_type, ...), read back as `dict(node.properties)` with `id` included, through this
  module's own `_RegulatoryInstrumentNode` structural Protocol.
- Role: properties are `name`/`confidence`, and the cached `embedding` when one exists (a curated
  artifact carries it from the export; an ingest baseline does not).
- Requirement: properties are `text`/`type`/`confidence`/`role_id`; `role_id` is bookkeeping, not
  an Edge Catalog relationship, and is carried through unchanged when present.
- Obligation: properties are `text`/`confidence` only; there is no `source_ref` because
  provenance is transitive.
- Capability: properties are `name`/`confidence`, `description` when set, and the cached
  `embedding` when one exists. `description` is omitted from the returned `BaselineNode.properties`
  entirely when absent.
- `DEFINES`/`EXPRESSES` edges carry a `source_ref` property; `HAS`/`SATISFIED_BY`/`REQUIRES` edges
  carry no properties.

The reader issues twenty-two fixed queries: one RegulatoryInstrument query, one per node label for
Role/Requirement/Obligation/Capability/Policy/Standard/Control/PracticeArea/RiskPath (nine), two
provenance-edge queries (`DEFINES`, `EXPRESSES`), six bare-edge queries (`HAS`, `SATISFIED_BY`,
`REQUIRES`, `GOVERNED_BY`, `SUPPORTED_BY`, `IMPLEMENTED_BY`) and four classification-edge queries
(`COVERS`, `OWNS`, `MITIGATED_BY`, `VERIFIED_BY`). Each relationship type has its own query so its
Python-side literal is fixed by which query produced the row, never parsed from a returned string;
a combined query with a runtime `type(e)` dispatch would need an unforced narrowing cast.

`_HANDLED_NODE_LABELS`/`_HANDLED_EDGE_TYPES` are a second, independent, hand-maintained record of
the labels and edge types this module reads. `test_graph_reader_vocabulary_drift.py`
compares them against `ps_service.ingestion.adapters.internal_seed.models.NodeLabel`/`EdgeType`, so
a vocabulary addition that forgets to extend these queries fails a test instead of silently
dropping data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, cast

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
_ROLE_QUERY = "MATCH (n:Role) RETURN n.id, n.name, n.confidence, n.embedding"
_REQUIREMENT_QUERY = "MATCH (n:Requirement) RETURN n.id, n.text, n.type, n.confidence, n.role_id"
_OBLIGATION_QUERY = "MATCH (n:Obligation) RETURN n.id, n.text, n.confidence"
_CAPABILITY_QUERY = (
    "MATCH (n:Capability) RETURN n.id, n.name, n.confidence, n.description, n.embedding"
)
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

# Policy/Standard/Control and the governance edges. Result sets are empty for an
# external-sourced baseline. The governance read takes every authored node, whatever its
# `status`, because an imported tree arrives `draft` and is written through the draft import.
_POLICY_ALL_QUERY = "MATCH (n:Policy) RETURN n"
_STANDARD_ALL_QUERY = "MATCH (n:Standard) RETURN n"
_CONTROL_ALL_QUERY = "MATCH (n:Control) RETURN n"
_GOVERNED_BY_QUERY = "MATCH (s:Capability)-[:GOVERNED_BY]->(t:Policy) RETURN s.id, t.id"
_SUPPORTED_BY_QUERY = "MATCH (s:Policy)-[:SUPPORTED_BY]->(t:Standard) RETURN s.id, t.id"
_IMPLEMENTED_BY_QUERY = "MATCH (s:Standard)-[:IMPLEMENTED_BY]->(t:Control) RETURN s.id, t.id"

# PracticeArea/RiskPath nodes. Result sets are empty for a baseline with no authored
# classification-layer content (same tolerance as Policy/Standard/Control).
_PRACTICE_AREA_QUERY = (
    "MATCH (n:PracticeArea) RETURN n.id, n.name, n.status, n.description, n.version, n.owner_id"
)
_RISK_PATH_QUERY = (
    "MATCH (n:RiskPath) RETURN n.id, n.name, n.status, n.description, n.risk_type, n.version"
)

# COVERS/OWNS/MITIGATED_BY/VERIFIED_BY classification edges. Result sets are empty for a
# baseline with no authored classification-layer content (same tolerance as the governance
# edges above).
_COVERS_QUERY = "MATCH (s:PracticeArea)-[:COVERS]->(t:Capability) RETURN s.id, t.id"
_OWNS_QUERY = "MATCH (s:PracticeArea)-[:OWNS]->(t:Policy) RETURN s.id, t.id"
_MITIGATED_BY_QUERY = "MATCH (s:RiskPath)-[:MITIGATED_BY]->(t:Capability) RETURN s.id, t.id"
_VERIFIED_BY_QUERY = "MATCH (s:RiskPath)-[:VERIFIED_BY]->(t:Control) RETURN s.id, t.id"

BASELINE_QUERIES: dict[str, str] = {
    "regulatory_instrument": _REGULATORY_INSTRUMENT_QUERY,
    "role": _ROLE_QUERY,
    "requirement": _REQUIREMENT_QUERY,
    "obligation": _OBLIGATION_QUERY,
    "capability": _CAPABILITY_QUERY,
    "defines": _DEFINES_QUERY,
    "expresses": _EXPRESSES_QUERY,
    "has": _HAS_QUERY,
    "satisfied_by": _SATISFIED_BY_QUERY,
    "requires": _REQUIRES_QUERY,
    "policy": _POLICY_ALL_QUERY,
    "standard": _STANDARD_ALL_QUERY,
    "control": _CONTROL_ALL_QUERY,
    "governed_by": _GOVERNED_BY_QUERY,
    "supported_by": _SUPPORTED_BY_QUERY,
    "implemented_by": _IMPLEMENTED_BY_QUERY,
    "practice_area": _PRACTICE_AREA_QUERY,
    "risk_path": _RISK_PATH_QUERY,
    "covers": _COVERS_QUERY,
    "owns": _OWNS_QUERY,
    "mitigated_by": _MITIGATED_BY_QUERY,
    "verified_by": _VERIFIED_BY_QUERY,
}
"""Every fixed query `read_baseline_graph` issues, by a short name.

A reader that answers them from something other than a live graph (Restore reads a parsed
artifact) is keyed by these; `tests/restore/test_baseline_reader.py` pins that the dict is
complete against this module's own `_*_QUERY` constants.
"""

# Drift guard: a hand-maintained record of exactly which labels/edge types this module reads,
# compared against internal_seed.models.NodeLabel/EdgeType by
# test_graph_reader_vocabulary_drift.py. Deliberately not derived from the query strings above:
# the point is to catch a label/edge type added to NodeLabel/EdgeType without also updating this
# module's queries (and this constant).
_HANDLED_NODE_LABELS: frozenset[str] = frozenset(
    {
        "RegulatoryInstrument",
        "Role",
        "Requirement",
        "Obligation",
        "Capability",
        "Policy",
        "Standard",
        "Control",
        "PracticeArea",
        "RiskPath",
    }
)
_HANDLED_EDGE_TYPES: frozenset[str] = frozenset(
    {
        "DEFINES",
        "EXPRESSES",
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
        "GOVERNED_BY",
        "SUPPORTED_BY",
        "IMPLEMENTED_BY",
        "COVERS",
        "OWNS",
        "MITIGATED_BY",
        "VERIFIED_BY",
    }
)


class _RegulatoryInstrumentNode(Protocol):
    """Structural stand-in for the `falkordb.Node` a `RETURN n` query returns.

    Only `.properties` is read, so a hand-written test fake needs only that one attribute. This
    component keeps its own copy rather than importing the Domain Mapper's.
    """

    @property
    def properties(self) -> dict[str, object]: ...


def read_baseline_graph(
    baseline_graph: GraphHandle,
    regulatory_instrument_id: str,
) -> BaselineGraph:
    """Read one regulation's complete `{short}_baseline` graph into a `BaselineGraph`.

    Policy/Standard/Control nodes are read at every `status` with every scalar property, because
    an imported tree arrives `draft` and `governance_import` writes it.

    Args:
        baseline_graph: Read handle on the regulation's `{short}_baseline` graph, already
            selected by the caller; this function does no graph selection of its own.
        regulatory_instrument_id: Id of the RegulatoryInstrument whose node and
            `DEFINES`/`EXPRESSES` edges are read.

    Returns:
        A `BaselineGraph` holding the RegulatoryInstrument properties, the node tuples, and the
        provenance, bare, governance and classification edge tuples. A collection with no
        matching rows is an empty tuple, so a baseline with no Obligation/Capability nodes (a
        regulation whose derivation matched nothing) yields empty tuples for those fields and for
        every edge collection that would reference them.

    Raises:
        Exception: Whatever `baseline_graph.query` raises (for example a Redis error) propagates
            unchanged.

    Side Effects:
        Issues read-only queries against `baseline_graph`; writes nothing.
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
    policy_nodes = _read_governance_nodes(baseline_graph, _POLICY_ALL_QUERY)
    standard_nodes = _read_governance_nodes(baseline_graph, _STANDARD_ALL_QUERY)
    control_nodes = _read_governance_nodes(baseline_graph, _CONTROL_ALL_QUERY)
    governance_edges = _read_governance_edges(baseline_graph)
    practice_area_nodes = _read_practice_area_nodes(baseline_graph)
    risk_path_nodes = _read_risk_path_nodes(baseline_graph)
    classification_edges = _read_classification_edges(baseline_graph)

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
        practice_area_nodes=practice_area_nodes,
        risk_path_nodes=risk_path_nodes,
        classification_edges=classification_edges,
    )


def _read_regulatory_instrument_properties(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> dict[str, object]:
    """Read the RegulatoryInstrument node back as a plain properties dict (`id` included).

    An absent node yields an empty dict rather than raising: this reader only reports what is
    present, and whether a missing RegulatoryInstrument aborts the merge is the merge's decision.
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
    """Read every Role node with its `name`, `confidence` and cached `embedding` if any.

    `embedding` is `None` when the Role has none (an ingest baseline never carries one; a curated
    artifact does, from the export's backfill); it is never `()` and absence never raises.
    """
    result = baseline_graph.query(_ROLE_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, name, confidence, embedding = row
        raw_embedding = cast("list[float] | None", embedding)
        nodes.append(
            BaselineNode(
                id=cast("str", node_id),
                properties={"name": cast("str", name), "confidence": cast("float", confidence)},
                embedding=tuple(raw_embedding) if raw_embedding is not None else None,
            )
        )
    return tuple(nodes)


def _read_requirement_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    """Read every Requirement node, omitting `role_id` when the graph returns `NULL`.

    `role_id` is bookkeeping and not always present (an internal-source Requirement can lack
    one), so it is added to `properties` only when it is not `None`; a `None` is never written
    into the properties.
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
    """Read every Obligation node with its `text` and `confidence`."""
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
    """Read every Capability node, including its cached `embedding` if any.

    `embedding` is `None` when the Capability has no cached embedding; it is never `()` and
    absence never raises. `graph_writer.backfill_incoming_capability_embeddings` sets it on this
    baseline graph once `dedup.dedupe_canonical_nodes` has computed one.
    """
    result = baseline_graph.query(_CAPABILITY_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, name, confidence, description, embedding = row
        properties: dict[str, str | float] = {
            "name": cast("str", name),
            "confidence": cast("float", confidence),
        }
        if description is not None:
            properties["description"] = cast("str", description)
        raw_embedding = cast("list[float] | None", embedding)
        nodes.append(
            BaselineNode(
                id=cast("str", node_id),
                properties=properties,
                embedding=tuple(raw_embedding) if raw_embedding is not None else None,
            )
        )
    return tuple(nodes)


def _read_provenance_edges(
    baseline_graph: GraphHandle, regulatory_instrument_id: str
) -> tuple[ProvenanceEdge, ...]:
    """Read the `DEFINES` then `EXPRESSES` provenance edges from the RegulatoryInstrument.

    `DEFINES` targets Role, `EXPRESSES` targets Requirement. Each is read via
    its own fixed-relationship-type query, so `relationship_type` is always a
    Python literal known at the call site, never parsed from a returned
    string.
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
    `_read_provenance_edges`. Endpoint ids are baseline-local; rewiring a
    `REQUIRES` edge's Capability endpoint onto its canonical id is
    `dedup`/`graph_writer`'s job, not this reader's. Obligation, Role,
    and Requirement endpoints are passthrough, so their baseline-local
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


def _read_governance_nodes(baseline_graph: GraphHandle, query: str) -> tuple[BaselineNode, ...]:
    """Read every Policy/Standard/Control node `query` returns, whatever its `status`.

    Returns each node's full scalar property map so an imported draft keeps its authored
    content. `id` is the node identity and `embedding` is carried separately (restore reads the
    artifact-supplied vectors off the parsed artifact), so neither is left in `properties`.
    Only `str`, `int` and `float` property values are kept.
    """
    result = baseline_graph.query(query)
    rows = cast("list[list[_RegulatoryInstrumentNode]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        raw = row[0].properties
        properties: dict[str, str | float] = {
            key: value
            for key, value in raw.items()
            if key not in {"id", "embedding"} and isinstance(value, str | int | float)
        }
        nodes.append(BaselineNode(id=cast("str", raw["id"]), properties=properties))
    return tuple(nodes)


def _read_governance_edges(baseline_graph: GraphHandle) -> tuple[BareEdge, ...]:
    """Read the `GOVERNED_BY`, `SUPPORTED_BY`, and `IMPLEMENTED_BY` edges.

    `GOVERNED_BY` (Capability -> Policy), then `SUPPORTED_BY` (Policy ->
    Standard), then `IMPLEMENTED_BY` (Standard -> Control) -- each read via
    its own fixed-relationship-type query, same reasoning as
    `_read_bare_edges`. Endpoint ids are baseline-local; rewiring a
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


def _read_practice_area_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    """Read every PracticeArea node. Empty for a baseline with no authored content.

    Properties are `name`/`status` (required) and, when set, `description`/`version`/`owner_id`;
    an unset optional property is omitted from the dict.
    """
    result = baseline_graph.query(_PRACTICE_AREA_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, name, status, description, version, owner_id = row
        properties: dict[str, str | float] = {
            "name": cast("str", name),
            "status": cast("str", status),
        }
        if description is not None:
            properties["description"] = cast("str", description)
        if version is not None:
            properties["version"] = cast("str", version)
        if owner_id is not None:
            properties["owner_id"] = cast("str", owner_id)
        nodes.append(BaselineNode(id=cast("str", node_id), properties=properties))
    return tuple(nodes)


def _read_risk_path_nodes(baseline_graph: GraphHandle) -> tuple[BaselineNode, ...]:
    """Read every RiskPath node. Empty for a baseline with no authored content.

    Properties are `name`/`status` (required) and, when set, `description`/`risk_type`/`version`;
    an unset optional property is omitted from the dict, as in `_read_practice_area_nodes`.
    """
    result = baseline_graph.query(_RISK_PATH_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    nodes: list[BaselineNode] = []
    for row in rows:
        node_id, name, status, description, risk_type, version = row
        properties: dict[str, str | float] = {
            "name": cast("str", name),
            "status": cast("str", status),
        }
        if description is not None:
            properties["description"] = cast("str", description)
        if risk_type is not None:
            properties["risk_type"] = cast("str", risk_type)
        if version is not None:
            properties["version"] = cast("str", version)
        nodes.append(BaselineNode(id=cast("str", node_id), properties=properties))
    return tuple(nodes)


def _read_classification_edges(baseline_graph: GraphHandle) -> tuple[BareEdge, ...]:
    """Read `COVERS`/`OWNS`/`MITIGATED_BY`/`VERIFIED_BY` edges.

    Empty for a baseline with no authored classification-layer content, with no exception
    raised (same contract as `_read_governance_edges`). It loops over `(literal, query)` pairs
    rather than writing one block per type, to keep the function's cyclomatic complexity low.
    `relationship_type` is still always one of the four literals below, never parsed from a
    returned Cypher type string.
    """
    edges: list[BareEdge] = []
    for relationship_type, query in (
        ("COVERS", _COVERS_QUERY),
        ("OWNS", _OWNS_QUERY),
        ("MITIGATED_BY", _MITIGATED_BY_QUERY),
        ("VERIFIED_BY", _VERIFIED_BY_QUERY),
    ):
        result = baseline_graph.query(query)
        for row in cast("list[list[object]]", result.result_set):
            source_id, target_id = row
            edges.append(
                BareEdge(
                    relationship_type=cast(
                        'Literal["COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY"]',
                        relationship_type,
                    ),
                    source_id=cast("str", source_id),
                    target_id=cast("str", target_id),
                )
            )
    return tuple(edges)
