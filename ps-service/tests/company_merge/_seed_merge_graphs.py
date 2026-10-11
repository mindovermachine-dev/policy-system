"""Graph doubles that run the real `merge_baseline_graph` over a real ingested seed (issue #227).

`BaselineGraphReader` answers `graph_reader.read_baseline_graph`'s queries from the state a real
`ingest_internal_regulatory_instrument` left in a gateway rig's in-memory `{short}_baseline`
graph, so the merge reads exactly the nodes and edges the ingestion minted. `SingleTenantStore`
is the stateful `policy_system` stand-in: it interprets the fixed write statements
`company_merge.graph_writer` issues (node `MERGE`, `MERGE ... ON CREATE SET`, edge `MERGE`) and
the existence reads, and keeps every statement it was given. `OneHotEmbeddingCaller` is the LLM
boundary: every distinct text gets its own orthogonal vector, so no two Capabilities converge.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.ingestion.adapters.internal_seed.adapter import InternalSeedIngestionAdapter
from ps_service.ingestion.adapters.internal_seed.persist import (
    ingest_internal_regulatory_instrument,
)

if TYPE_CHECKING:
    from graph_gateway._fakes import GatewayRig, InMemoryGraph

    from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed

EMBED_MODEL = "fake-embed-model"
SIMILARITY_THRESHOLD = 0.85
_EMBEDDING_DIMENSION = 512


@dataclass(frozen=True)
class _Node:
    """The `.properties` view of a `falkordb.Node` that `RETURN n` yields."""

    properties: dict[str, object]


@dataclass(frozen=True)
class _Result:
    result_set: list[object]


def parse_seed(document: dict[str, object]) -> InternalRegulationSeed:
    """Parse an intake document with the real adapter (schema and model validation)."""
    return InternalSeedIngestionAdapter().parse_seed(document)


def ingest_seed(rig: GatewayRig, seed: InternalRegulationSeed) -> str:
    """Run the real internal ingestion over `rig`; return the `{short}_baseline` graph name."""
    instrument = next(n for n in seed.nodes if n.label == "RegulatoryInstrument")
    short_name = instrument.id.rpartition("-")[0].lower()
    audit_event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(audit_event_id)
    ingest_internal_regulatory_instrument(
        seed,
        submitter=rig.gateway,
        native_graph_name=f"{short_name}_native",
        baseline_graph_name=f"{short_name}_baseline",
        audit_event_id=audit_event_id,
    )
    return f"{short_name}_baseline"


class BaselineGraphReader:
    """A `GraphHandle` answering the baseline reader's queries from a gateway rig graph."""

    def __init__(self, graph: InMemoryGraph) -> None:
        self._graph = graph

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Answer one reader query from the graph's current nodes and edges."""
        if "WHERE n.embedding IS NULL SET n.embedding" in q:
            return self._cache_embedding(params)
        for label in ("Policy", "Standard", "Control"):
            if q == f"MATCH (n:{label}) RETURN n":
                return _Result([[_Node(props)] for props in self._nodes(label)])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _Result([[_Node(props)] for props in self._nodes("RegulatoryInstrument")])
        for relationship in ("DEFINES", "EXPRESSES"):
            if f"[e:{relationship}]" in q:
                return _Result(
                    [[tgt, props.get("source_ref")] for _, tgt, props in self._edges(relationship)]
                )
        for relationship in (
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
        ):
            if f"[:{relationship}]" in q:
                return _Result([[src, tgt] for src, tgt, _ in self._edges(relationship)])
        return _Result(self._column_rows(q))

    def _cache_embedding(self, params: dict[str, object] | None) -> _Result:
        """The merge's write-back of a freshly computed incoming Capability embedding."""
        assert params is not None
        node = self._graph.nodes.get(("Capability", cast("str", params["id"])))
        if node is not None and node.get("embedding") is None:
            node["embedding"] = params["embedding"]
        return _Result([])

    def _column_rows(self, q: str) -> list[object]:
        """Rows for the `RETURN n.a, n.b, ...` node reads, one column per named property."""
        match = re.match(r"^MATCH \(n:(\w+)\) RETURN (.+)$", q)
        if match is None:
            raise AssertionError(f"unexpected baseline query: {q!r}")
        columns = [column.strip().removeprefix("n.") for column in match.group(2).split(",")]
        return [[props.get(column) for column in columns] for props in self._nodes(match.group(1))]

    def _nodes(self, label: str) -> list[dict[str, object]]:
        return [
            {"id": node_id, **props}
            for (node_label, node_id), props in self._graph.nodes.items()
            if node_label == label
        ]

    def _edges(self, relationship: str) -> list[tuple[str, str, dict[str, object]]]:
        return [
            (src_id, tgt_id, props)
            for (rel, _src_label, src_id, _tgt_label, tgt_id, _identity), props in (
                self._graph.edges.items()
            )
            if rel == relationship
        ]


@dataclass
class SingleTenantStore:
    """A stateful `policy_system` stand-in that interprets `graph_writer`'s fixed statements."""

    nodes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)
    statements: list[str] = field(default_factory=list)
    params_seen: list[dict[str, object]] = field(default_factory=list)

    _NODE_WRITE = re.compile(
        r"^MERGE \(n:(\w+) \{id: \$id\}\) (ON CREATE SET|SET) n \+= \$properties$"
    )
    _EDGE_WRITE = re.compile(
        r"^MATCH \(s:(\w+) \{id: \$source_id\}\), \(t:(\w+) \{id: \$target_id\}\) "
        r"MERGE \(s\)-\[:(\w+)\]->\(t\)$"
    )
    _INDEX_READ = re.compile(r"^MATCH \(n:(\w+)\) RETURN n\.id, n\.(\w+), n\.embedding$")

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Apply a write or answer a read; unknown statements are recorded and answered `[[0]]`."""
        self.statements.append(q)
        self.params_seen.append(dict(params or {}))
        node_write = self._NODE_WRITE.match(q)
        if node_write is not None and params is not None:
            self._write_node(node_write.group(1), node_write.group(2), params)
            return _Result([])
        edge_write = self._EDGE_WRITE.match(q)
        if edge_write is not None and params is not None:
            self._write_edge(edge_write.group(3), params)
            return _Result([])
        return _Result(self._read(q, params))

    def _write_node(self, label: str, mode: str, params: dict[str, object]) -> None:
        key = (label, cast("str", params["id"]))
        properties = dict(cast("dict[str, object]", params["properties"]))
        if mode == "SET":
            self.nodes.setdefault(key, {}).update(properties)
        elif key not in self.nodes:
            self.nodes[key] = properties

    def _write_edge(self, relationship: str, params: dict[str, object]) -> None:
        source, target = cast("str", params["source_id"]), cast("str", params["target_id"])
        known = {node_id for _, node_id in self.nodes}
        if source in known and target in known:
            self.edges.add((relationship, source, target))

    def _read(self, q: str, params: dict[str, object] | None) -> list[object]:
        if q == "UNWIND $ids AS id MATCH (n {id: id}) RETURN id":
            requested = cast("list[str]", (params or {})["ids"])
            known = {node_id for _, node_id in self.nodes}
            return [[node_id] for node_id in requested if node_id in known]
        if q == "MATCH (o:Obligation) WHERE o.id IN $ids RETURN o.id":
            requested = cast("list[str]", (params or {})["ids"])
            return [[i] for i in requested if ("Obligation", i) in self.nodes]
        index_read = self._INDEX_READ.match(q)
        if index_read is not None:
            label, text_key = index_read.groups()
            return [
                [node_id, props.get(text_key), props.get("embedding")]
                for (node_label, node_id), props in self.nodes.items()
                if node_label == label
            ]
        if "MERGED_INTO" in q or "MergedObligation" in q:
            return []
        return [[0]]

    def nodes_of(self, label: str) -> dict[str, dict[str, object]]:
        """The stored properties of every `label` node, by id."""
        return {
            node_id: props
            for (node_label, node_id), props in self.nodes.items()
            if node_label == label
        }

    def edge_count(self, relationship: str) -> int:
        """How many `relationship` edges are stored."""
        return sum(1 for rel, _, _ in self.edges if rel == relationship)


class OneHotEmbeddingCaller:
    """An `EmbeddingCaller` giving every distinct text its own orthogonal vector."""

    def __init__(self) -> None:
        self._index_by_text: dict[str, int] = {}
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        """Embed `inputs` (always one text per call, as the dedup pass issues them)."""
        del timeout
        text = inputs[0]
        self.calls.append(text)
        index = self._index_by_text.setdefault(text, len(self._index_by_text))
        vector = [0.0] * _EMBEDDING_DIMENSION
        vector[index] = 1.0
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )
