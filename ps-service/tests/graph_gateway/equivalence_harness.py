"""Test-side harness proving two write paths build the same graph (issue #207, AC-RD-009).

Each writer migration (#208-#214) moves a writer from "write straight to FalkorDB" to "submit
`MutationGroup`s through the gateway". The graph that results must be the same. This harness
decides that by comparing the canonical digest of the two graphs (`digest.canonical_digest`),
which ignores FalkorDB internal ids and insertion order.

Usage::

    artifact = load_artifact(Path("curated-content/CRA-1.0/native.json"))
    result = run_equivalence(artifact, old_path, new_path)
    assert result.passed, result

`old_path` and `new_path` are callables `(Artifact) -> GraphHandle`: each writes the artifact the
way the writer does today, respectively through the gateway, and returns the graph it built.
`SyntheticWriter` is a stand-in with both paths, used until the real writers are wired to the
gateway; it lives in tests because `ps-test-support` has no `ps_service` dependency and nothing
here may ship in the production image.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from graph_gateway._fakes import GatewayRig, InMemoryGraph
from ps_service.graph_gateway import cypher
from ps_service.graph_gateway.digest import canonical_digest, element_hashes
from ps_service.graph_gateway.models import (
    MutationGroup,
    NodeRef,
    Primitive,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.ingestion.falkordb_client import GraphHandle

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_GRAPH = "equivalence"
_GROUP_SIZE = 200


@dataclass(frozen=True)
class ArtifactNode:
    """A node of a curated artifact: label, id, properties and an optional embedding."""

    label: str
    id: str
    properties: Mapping[str, object]
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class ArtifactEdge:
    """A relationship of a curated artifact."""

    source_label: str
    source_id: str
    target_label: str
    target_id: str
    relationship_type: str
    properties: Mapping[str, object]

    @property
    def identity(self) -> str:
        """The deterministic edge identity both paths use (the artifact carries none)."""
        return f"{self.relationship_type}:{self.source_id}->{self.target_id}"


@dataclass(frozen=True)
class Artifact:
    """A curated artifact, shaped like `curated-content/*/native.json`."""

    nodes: tuple[ArtifactNode, ...]
    edges: tuple[ArtifactEdge, ...]


@dataclass(frozen=True)
class ElementDifferences:
    """How many elements one graph holds that the other does not, per kind (hashes, no content)."""

    nodes_only_in_old: int = 0
    nodes_only_in_new: int = 0
    edges_only_in_old: int = 0
    edges_only_in_new: int = 0


@dataclass(frozen=True)
class EquivalenceResult:
    """The outcome of comparing the old and the new path.

    It carries digests and counts only: never a property value, so a failure can be logged.
    """

    passed: bool
    old_digest: str
    new_digest: str
    node_count: int
    edge_count: int
    differences: ElementDifferences = ElementDifferences()


WritePath = Callable[[Artifact], "GraphHandle"]


def load_artifact(path: Path) -> Artifact:
    """Read an artifact from a JSON file."""
    return parse_artifact(json.loads(path.read_text(encoding="utf-8")))


def parse_artifact(document: object) -> Artifact:
    """Build an artifact from its already-parsed JSON document."""
    raw = cast("Mapping[str, list[Mapping[str, object]]]", document)
    return Artifact(
        nodes=tuple(_node(entry) for entry in raw["nodes"]),
        edges=tuple(_edge(entry) for entry in raw["edges"]),
    )


def _node(entry: Mapping[str, object]) -> ArtifactNode:
    properties = dict(cast("Mapping[str, object]", entry["properties"]))
    node_id = str(properties.pop("id"))
    embedding = properties.pop("embedding", None)
    return ArtifactNode(
        label=str(entry["label"]),
        id=node_id,
        properties=properties,
        embedding=None if embedding is None else tuple(cast("list[float]", embedding)),
    )


def _edge(entry: Mapping[str, object]) -> ArtifactEdge:
    return ArtifactEdge(
        source_label=str(entry["source_label"]),
        source_id=str(entry["source_id"]),
        target_label=str(entry["target_label"]),
        target_id=str(entry["target_id"]),
        relationship_type=str(entry["relationship_type"]),
        properties=cast("Mapping[str, object]", entry.get("properties", {})),
    )


def _upsert_node(node: ArtifactNode) -> UpsertNode:
    return UpsertNode(
        label=node.label, id=node.id, properties=dict(node.properties), embedding=node.embedding
    )


def _upsert_edge(edge: ArtifactEdge) -> UpsertEdge:
    return UpsertEdge(
        type=edge.relationship_type,
        identity=edge.identity,
        source=NodeRef(label=edge.source_label, id=edge.source_id),
        target=NodeRef(label=edge.target_label, id=edge.target_id),
        properties=dict(edge.properties),
    )


class SyntheticWriter:
    """Stand-in for a real writer: an old path (direct `UNWIND`) and a new path (the gateway)."""

    def old_path(self, artifact: Artifact) -> InMemoryGraph:
        """Write straight to a fresh graph with the gateway's `UNWIND` templates."""
        graph = InMemoryGraph()
        by_label: dict[str, list[UpsertNode]] = defaultdict(list)
        for node in artifact.nodes:
            by_label[node.label].append(_upsert_node(node))
        for label, nodes in by_label.items():
            graph.query(cypher.upsert_node_query(label), {"rows": cypher.upsert_node_rows(nodes)})
        by_shape: dict[tuple[str, str, str], list[UpsertEdge]] = defaultdict(list)
        for edge in artifact.edges:
            key = (edge.relationship_type, edge.source_label, edge.target_label)
            by_shape[key].append(_upsert_edge(edge))
        for edges in by_shape.values():
            graph.query(
                cypher.upsert_edge_query(edges[0]), {"rows": cypher.upsert_edge_rows(edges)}
            )
        return graph

    def new_path(self, artifact: Artifact) -> InMemoryGraph:
        """Submit the artifact as `MutationGroup`s through a gateway over fresh fakes."""
        primitives: list[Primitive] = [_upsert_node(node) for node in artifact.nodes]
        primitives.extend(_upsert_edge(edge) for edge in artifact.edges)
        rig = GatewayRig()
        try:
            for start in range(0, len(primitives), _GROUP_SIZE):
                rig.gateway.submit_group(
                    MutationGroup(
                        graph=_GRAPH,
                        audit_event_id=_AUDIT_EVENT_ID,
                        primitives=tuple(primitives[start : start + _GROUP_SIZE]),
                    )
                )
        finally:
            rig.close()
        return rig.graphs.open(_GRAPH)


def run_equivalence(
    artifact: Artifact, old_path: WritePath, new_path: WritePath
) -> EquivalenceResult:
    """Write `artifact` by both paths and compare the canonical digests of the two graphs."""
    old_graph, new_graph = old_path(artifact), new_path(artifact)
    old_digest, new_digest = canonical_digest(old_graph), canonical_digest(new_graph)
    return EquivalenceResult(
        passed=old_digest == new_digest,
        old_digest=old_digest,
        new_digest=new_digest,
        node_count=len(artifact.nodes),
        edge_count=len(artifact.edges),
        differences=ElementDifferences()
        if old_digest == new_digest
        else _differences(old_graph, new_graph),
    )


def _differences(old_graph: GraphHandle, new_graph: GraphHandle) -> ElementDifferences:
    old, new = element_hashes(old_graph), element_hashes(new_graph)
    old_nodes, new_nodes = Counter(old.nodes), Counter(new.nodes)
    old_edges, new_edges = Counter(old.edges), Counter(new.edges)
    return ElementDifferences(
        nodes_only_in_old=(old_nodes - new_nodes).total(),
        nodes_only_in_new=(new_nodes - old_nodes).total(),
        edges_only_in_old=(old_edges - new_edges).total(),
        edges_only_in_new=(new_edges - old_edges).total(),
    )
