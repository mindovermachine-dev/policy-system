# pyright: reportPrivateUsage=false
"""Frozen copy of the internal-seed writer as it was before issue #208: the equivalence reference.

It issues the same Cypher the pre-migration `persist.py` issued (`MERGE ... SET n += $properties`,
`MATCH ... MERGE ... SET r += $properties`) against a `LegacyRecordingGraph`, which records every
call and keeps the node and edge sets those calls would have produced (a `MATCH` that finds no
endpoint writes nothing). Only the node/edge parameters of the calls are read; the Cypher text
yields the label and relationship type only. The property builders and canonical-id minting it
shares with the new writer are the ones #208 leaves untouched (PLAN: `_*_properties` unchanged).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from graph_gateway._fakes import InMemoryGraph

from ps_service.ingestion.adapters.internal_seed import persist as new_writer

if TYPE_CHECKING:
    from ps_service.graph_gateway.models import MutationGroup
    from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed

_NODE_QUERY = re.compile(r"^MERGE \(n:(\w+) \{id: \$id\}\) SET n \+= \$properties$")
_EDGE_QUERY = re.compile(
    r"^MATCH \(a:(\w+) \{id: \$from_id\}\), \(b:(\w+) \{id: \$to_id\}\) "
    r"MERGE \(a\)-\[r:(\w+)\]->\(b\) SET r \+= \$properties$"
)


class LegacyWriteTarget(Protocol):
    """What the frozen legacy writer needs: a recording graph here, a real FalkorDB handle live."""

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run one Cypher statement."""
        ...


type NodeFact = tuple[str, str, frozenset[tuple[str, object]]]
type EdgeFact = tuple[str, str, str, str, str, frozenset[tuple[str, object]]]


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
        assert params is not None
        properties = cast("dict[str, object]", params["properties"])
        if node_match := _NODE_QUERY.match(q):
            label = node_match.group(1)
            node_id = cast("str", params["id"])
            self.node_calls.append((label, node_id, frozenset(properties.items())))
            self.nodes.setdefault((label, node_id), {}).update(properties)
            return _Result()
        edge_match = _EDGE_QUERY.match(q)
        assert edge_match is not None, q
        source_label, target_label, edge_type = edge_match.groups()
        source_id, target_id = cast("str", params["from_id"]), cast("str", params["to_id"])
        self.edge_calls.append(
            (
                edge_type,
                source_label,
                source_id,
                target_label,
                target_id,
                frozenset(properties.items()),
            )
        )
        if (source_label, source_id) in self.nodes and (target_label, target_id) in self.nodes:
            key = (edge_type, source_label, source_id, target_label, target_id)
            self.edges.setdefault(key, {}).update(properties)
        return _Result()

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
        ), properties in self.edges.items():
            identity = f"{edge_type}:{source_id}->{target_id}"
            key = (edge_type, source_label, source_id, target_label, target_id, identity)
            graph.edges[key] = {"identity": identity, **properties}
            graph.edge_internal_id(key)
        return graph

    def node_facts(self) -> Counter[NodeFact]:
        """Every node write the calls made, as a multiset."""
        return Counter(self.node_calls)

    def edge_facts(self) -> Counter[EdgeFact]:
        """Every edge write whose two endpoints existed, as a multiset (others wrote nothing)."""
        kept = [
            call
            for call in self.edge_calls
            if (call[1], call[2]) in self.nodes and (call[3], call[4]) in self.nodes
        ]
        return Counter(kept)


def _merge_node(graph: LegacyWriteTarget, label: str, node_id: str, properties: object) -> None:
    graph.query(
        f"MERGE (n:{label} {{id: $id}}) SET n += $properties",
        params={"id": node_id, "properties": properties},
    )


def _merge_edge(
    graph: LegacyWriteTarget,
    edge_type: str,
    source: tuple[str, str],
    target: tuple[str, str],
    properties: object,
) -> None:
    graph.query(
        f"MATCH (a:{source[0]} {{id: $from_id}}), (b:{target[0]} {{id: $to_id}}) "
        f"MERGE (a)-[r:{edge_type}]->(b) SET r += $properties",
        params={"from_id": source[1], "to_id": target[1], "properties": properties},
    )


def write_legacy(
    seed: InternalRegulationSeed, native: LegacyWriteTarget, baseline: LegacyWriteTarget
) -> None:
    """The pre-#208 `_persist_native` + `_persist_baseline`, statement for statement."""
    index = new_writer._validate_and_index(seed)
    canonical = new_writer._mint_canonical_ids(seed, index)
    for node in seed.nodes:
        _merge_node(native, node.label, node.id, dict(node.properties))
    for edge in seed.edges:
        _merge_edge(
            native,
            edge.type,
            (edge.from_.label, edge.from_.id),
            (edge.to.label, edge.to.id),
            dict(edge.properties),
        )
    _write_baseline_nodes(baseline, seed, index, canonical)
    for edge in seed.edges:
        edge_properties = (
            {"source_ref": edge.properties["source_ref"]}
            if edge.type in ("DEFINES", "EXPRESSES")
            else {}
        )
        _merge_edge(
            baseline,
            edge.type,
            (edge.from_.label, canonical.resolve(edge.from_)),
            (edge.to.label, canonical.resolve(edge.to)),
            edge_properties,
        )


def _write_baseline_nodes(
    graph: LegacyWriteTarget,
    seed: InternalRegulationSeed,
    index: new_writer._SeedIndex,
    canonical: new_writer._CanonicalIds,
) -> None:
    status = new_writer._policy_status_by_local_id(seed)
    _merge_node(
        graph,
        "RegulatoryInstrument",
        canonical.regulatory_instrument_id,
        new_writer._regulatory_instrument_properties(index.regulatory_instrument),
    )
    for node in seed.nodes:
        if node.label == "Role":
            _merge_node(graph, "Role", canonical.role[node.id], new_writer._role_properties(node))
        elif node.label == "Capability":
            _merge_node(
                graph,
                "Capability",
                canonical.capability[node.id],
                new_writer._capability_properties(node),
            )
        elif node.label == "Policy":
            _merge_node(
                graph, "Policy", canonical.policy[node.id], new_writer._policy_properties(node)
            )
        elif node.label == "Standard":
            policy = index.standard_supporter[node.id]
            _merge_node(
                graph,
                "Standard",
                canonical.standard[node.id],
                new_writer._standard_properties(node, status[policy]),
            )
        elif node.label == "Control":
            standard = index.control_implementer[node.id]
            policy = index.standard_supporter[standard]
            _merge_node(
                graph,
                "Control",
                canonical.control[node.id],
                new_writer._control_properties(node, status[policy]),
            )
    for node in seed.nodes:
        if node.label == "Obligation":
            _merge_node(
                graph,
                "Obligation",
                canonical.obligation[node.id],
                new_writer._obligation_properties(node),
            )
        elif node.label == "Requirement":
            role_id_value = new_writer._resolve_requirement_role_id(node.id, index, canonical.role)
            _merge_node(
                graph,
                "Requirement",
                canonical.requirement[node.id],
                new_writer._requirement_properties(node, role_id_value),
            )
    for node in seed.nodes:
        if node.label == "PracticeArea":
            _merge_node(
                graph,
                "PracticeArea",
                canonical.practice_area[node.id],
                new_writer._practice_area_properties(node),
            )
        elif node.label == "RiskPath":
            _merge_node(
                graph,
                "RiskPath",
                canonical.risk_path[node.id],
                new_writer._risk_path_properties(node),
            )


def primitive_node_facts(group: MutationGroup) -> Counter[NodeFact]:
    """The node upserts of a gateway group, in the same shape as the legacy node calls."""
    return Counter(
        (p.label, p.id, frozenset(p.properties.items()))
        for p in group.primitives
        if p.op == "upsert_node"
    )


def primitive_edge_facts(group: MutationGroup) -> Counter[EdgeFact]:
    """The edge upserts of a gateway group, in the same shape as the legacy edge calls."""
    return Counter(
        (
            p.type,
            p.source.label,
            p.source.id,
            p.target.label,
            p.target.id,
            frozenset(p.properties.items()),
        )
        for p in group.primitives
        if p.op == "upsert_edge"
    )
