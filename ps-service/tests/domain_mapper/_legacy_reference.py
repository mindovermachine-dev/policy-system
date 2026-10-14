"""Frozen copy of the Domain Mapper's pre-gateway graph writer: the reference.

It issues the Cypher the pre-migration `graph_writer.py` issued (`MERGE ... SET n += $properties`;
`MATCH ... MERGE ... SET e.source_ref = $source_ref` for DEFINES/EXPRESSES; `MATCH ... MERGE` with
no `SET` for HAS/SATISFIED_BY/REQUIRES), in the same order, against a `LegacyRecordingGraph`. The
graph records every call and keeps the node and edge sets those calls would have produced: a
`MATCH` that finds no endpoint writes nothing, and a repeated `MERGE` folds onto the first. Only
the parameters of the calls are read; the Cypher text yields labels and relationship types only.
The legacy `_validate_role_references` pre-flight is not copied: it wrote nothing and the new
writer keeps it unchanged, so it is not a write shape.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from graph_gateway._fakes import InMemoryGraph
from ingestion.adapters.internal_seed._legacy_reference import (
    EdgeFact,
    NodeFact,
    primitive_edge_facts,
    primitive_node_facts,
)

from ps_service.domain_mapper.models import RoleDefinesEdge

if TYPE_CHECKING:
    from ps_service.domain_mapper.models import (
        CapabilityNode,
        CapabilityRequiresEdge,
        ObligationHasEdge,
        ObligationNode,
        RequirementExpressesEdge,
        RequirementNode,
        RequirementSatisfiedByEdge,
        RoleNode,
    )

__all__ = [
    "LegacyRecordingGraph",
    "LegacyWriteTarget",
    "primitive_edge_facts",
    "primitive_node_facts",
    "write_legacy_obligation_and_capability_graph",
    "write_legacy_role_and_requirement_graph",
]

_INSTRUMENT = "RegulatoryInstrument"
_NODE_QUERY = re.compile(r"^MERGE \(n:(\w+) \{id: \$id\}\) SET n \+= \$properties$")
_INSTRUMENT_EDGE_QUERY = re.compile(
    r"^MATCH \(r:RegulatoryInstrument \{id: \$regulatory_instrument_id\}\), "
    r"\(n:(\w+) \{id: \$target_id\}\) "
    r"MERGE \(r\)-\[e:(\w+)\]->\(n\) SET e\.source_ref = \$source_ref$"
)
_BARE_EDGE_QUERY = re.compile(
    r"^MATCH \(s:(\w+) \{id: \$source_id\}\), \(t:(\w+) \{id: \$target_id\}\) "
    r"MERGE \(s\)-\[:(\w+)\]->\(t\)$"
)


class LegacyWriteTarget(Protocol):
    """Where the frozen statements run: the recording graph here, a real FalkorDB graph live."""

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run one legacy statement."""
        ...


@dataclass
class _Result:
    result_set: list[object] = field(default_factory=list)


@dataclass
class LegacyRecordingGraph:
    """A legacy `GraphHandle`: records every write call and the graph those calls build."""

    node_calls: list[NodeFact] = field(default_factory=list)
    edge_calls: list[EdgeFact] = field(default_factory=list)
    nodes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    edges: dict[tuple[str, str, str, str, str], dict[str, object]] = field(default_factory=dict)

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Apply one frozen legacy statement; any other shape is a failure of the reference."""
        assert params is not None
        if node_match := _NODE_QUERY.match(q):
            label = node_match.group(1)
            node_id = cast("str", params["id"])
            properties = cast("dict[str, object]", params["properties"])
            self.node_calls.append((label, node_id, frozenset(properties.items())))
            self.nodes.setdefault((label, node_id), {}).update(properties)
        elif edge_match := _INSTRUMENT_EDGE_QUERY.match(q):
            target_label, edge_type = edge_match.groups()
            self._merge_edge(
                edge_type,
                (_INSTRUMENT, cast("str", params["regulatory_instrument_id"])),
                (target_label, cast("str", params["target_id"])),
                {"source_ref": params["source_ref"]},
            )
        elif bare_match := _BARE_EDGE_QUERY.match(q):
            source_label, target_label, edge_type = bare_match.groups()
            self._merge_edge(
                edge_type,
                (source_label, cast("str", params["source_id"])),
                (target_label, cast("str", params["target_id"])),
                {},
            )
        else:
            raise AssertionError(f"not a frozen legacy statement: {q}")
        return _Result()

    def _merge_edge(
        self,
        edge_type: str,
        source: tuple[str, str],
        target: tuple[str, str],
        properties: dict[str, object],
    ) -> None:
        self.edge_calls.append(
            (edge_type, source[0], source[1], target[0], target[1], frozenset(properties.items()))
        )
        if source in self.nodes and target in self.nodes:
            key = (edge_type, source[0], source[1], target[0], target[1])
            self.edges.setdefault(key, {}).update(properties)

    def as_gateway_graph(self) -> InMemoryGraph:
        """The graph the calls built, every edge stamped with the gateway's identity (D2)."""
        graph = InMemoryGraph()
        for (label, node_id), properties in self.nodes.items():
            graph.seed_node((label,), node_id, properties)
        for (
            edge_type,
            source_label,
            source_id,
            target_label,
            target_id,
        ), props in self.edges.items():
            identity = f"{edge_type}:{source_id}->{target_id}"
            key = (edge_type, source_label, source_id, target_label, target_id, identity)
            graph.edges[key] = {"identity": identity, **props}
            graph.edge_internal_id(key)
        return graph

    def final_node_facts(self) -> Counter[NodeFact]:
        """The nodes the calls left, once each, with every folded property."""
        return Counter(
            (label, node_id, frozenset(props.items()))
            for (label, node_id), props in self.nodes.items()
        )

    def final_edge_facts(self) -> Counter[EdgeFact]:
        """The edges the calls left (endpoints existed), once each, with their properties."""
        return Counter(
            (edge_type, source_label, source_id, target_label, target_id, frozenset(props.items()))
            for (
                edge_type,
                source_label,
                source_id,
                target_label,
                target_id,
            ), props in self.edges.items()
        )


def _merge_node(graph: LegacyWriteTarget, label: str, node_id: str, properties: object) -> None:
    graph.query(
        f"MERGE (n:{label} {{id: $id}}) SET n += $properties",
        params={"id": node_id, "properties": properties},
    )


def _merge_instrument_edge(
    graph: LegacyWriteTarget,
    relationship_type: str,
    target_label: str,
    regulatory_instrument_id: str,
    edge: RoleDefinesEdge | RequirementExpressesEdge,
) -> None:
    target_node_id = (
        edge.role_node_id if isinstance(edge, RoleDefinesEdge) else edge.requirement_node_id
    )
    graph.query(
        f"MATCH (r:{_INSTRUMENT} {{id: $regulatory_instrument_id}}), "
        f"(n:{target_label} {{id: $target_id}}) "
        f"MERGE (r)-[e:{relationship_type}]->(n) SET e.source_ref = $source_ref",
        params={
            "regulatory_instrument_id": regulatory_instrument_id,
            "target_id": target_node_id,
            "source_ref": edge.source_ref,
        },
    )


def _merge_bare_edge(
    graph: LegacyWriteTarget,
    relationship_type: str,
    source: tuple[str, str],
    target: tuple[str, str],
) -> None:
    graph.query(
        f"MATCH (s:{source[0]} {{id: $source_id}}), (t:{target[0]} {{id: $target_id}}) "
        f"MERGE (s)-[:{relationship_type}]->(t)",
        params={"source_id": source[1], "target_id": target[1]},
    )


def write_legacy_role_and_requirement_graph(
    graph: LegacyWriteTarget,
    regulatory_instrument_id: str,
    regulatory_instrument_properties: dict[str, object],
    role_nodes: tuple[RoleNode, ...],
    role_edges: tuple[RoleDefinesEdge, ...],
    requirement_nodes: tuple[RequirementNode, ...],
    requirement_edges: tuple[RequirementExpressesEdge, ...],
) -> None:
    """The pre-gateway `persist_role_and_requirement_graph` writes, statement for statement."""
    _merge_node(
        graph,
        _INSTRUMENT,
        regulatory_instrument_id,
        {k: v for k, v in regulatory_instrument_properties.items() if k != "id"},
    )
    for role in role_nodes:
        _merge_node(graph, "Role", role.id, role.properties)
    for role_edge in role_edges:
        _merge_instrument_edge(graph, "DEFINES", "Role", regulatory_instrument_id, role_edge)
    for requirement in requirement_nodes:
        _merge_node(graph, "Requirement", requirement.id, requirement.properties)
    for requirement_edge in requirement_edges:
        _merge_instrument_edge(
            graph, "EXPRESSES", "Requirement", regulatory_instrument_id, requirement_edge
        )


def write_legacy_obligation_and_capability_graph(
    graph: LegacyWriteTarget,
    obligation_nodes: tuple[ObligationNode, ...],
    has_edges: tuple[ObligationHasEdge, ...],
    satisfied_by_edges: tuple[RequirementSatisfiedByEdge, ...],
    capability_nodes: tuple[CapabilityNode, ...],
    requires_edges: tuple[CapabilityRequiresEdge, ...],
) -> None:
    """The pre-gateway `persist_obligation_and_capability_graph` writes, statement for statement."""
    for obligation in obligation_nodes:
        _merge_node(graph, "Obligation", obligation.id, obligation.properties)
    for capability in capability_nodes:
        _merge_node(graph, "Capability", capability.id, capability.properties)
    for has_edge in has_edges:
        _merge_bare_edge(
            graph,
            "HAS",
            ("Role", has_edge.role_node_id),
            ("Obligation", has_edge.obligation_node_id),
        )
    for satisfied_by_edge in satisfied_by_edges:
        _merge_bare_edge(
            graph,
            "SATISFIED_BY",
            ("Requirement", satisfied_by_edge.requirement_id),
            ("Obligation", satisfied_by_edge.obligation_node_id),
        )
    for requires_edge in requires_edges:
        _merge_bare_edge(
            graph,
            "REQUIRES",
            ("Obligation", requires_edge.obligation_node_id),
            ("Capability", requires_edge.capability_node_id),
        )
