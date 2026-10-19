"""Graph doubles that run the real `merge_baseline_graph` over a real ingested seed.

`BaselineGraphReader` answers `graph_reader.read_baseline_graph`'s queries from the state a real
`ingest_internal_regulatory_instrument` left in a gateway rig's in-memory `{short}_baseline`
graph, so the merge reads exactly the nodes and edges the ingestion minted. `SingleTenantStore`
is the `policy_system` read stand-in: it answers the existence, embedding-state and index reads
and keeps every statement it was given (a write statement is refused). `OneHotEmbeddingCaller` is
the LLM boundary: every distinct text gets its own orthogonal vector, so no two Capabilities
converge.
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

_WRITE_CLAUSE = re.compile(r"\b(MERGE|CREATE|SET|DELETE|REMOVE)\b")
_EMBEDDING_STATE = re.compile(
    r"^MATCH \(n:(\w+)\) WHERE n\.id IN \$ids RETURN n\.id, n\.embedding IS NULL$"
)
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


StoreResult = _Result
"""The result type of a `SingleTenantStore` query (public for its write-capable subclass)."""


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
        if _WRITE_CLAUSE.search(q):
            raise AssertionError(f"a write statement reached the baseline reader: {q}")
        embedding_state = _EMBEDDING_STATE.match(q)
        if embedding_state is not None:
            return self._embedding_state(embedding_state.group(1), params)
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

    def _embedding_state(self, label: str, params: dict[str, object] | None) -> _Result:
        """The embedding backfill's read: which requested ids exist, and have no embedding yet."""
        requested = cast("list[str]", (params or {})["ids"])
        return _Result(
            [
                [node_id, self._graph.nodes[(label, node_id)].get("embedding") is None]
                for node_id in requested
                if (label, node_id) in self._graph.nodes
            ]
        )

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
    """A `policy_system` read stand-in: the existence, embedding-state and index reads.

    Every merge write is a gateway group, so a write statement is refused. The old
    write interpreter is `_legacy_reference.SnapshotWriteStore` (Restore's interim writer).
    """

    nodes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    edges: set[tuple[str, str, str]] = field(default_factory=set)
    statements: list[str] = field(default_factory=list)
    params_seen: list[dict[str, object]] = field(default_factory=list)

    _EXISTING_READ = re.compile(r"^MATCH \(n:(\w+)\) WHERE n\.id IN \$ids RETURN n\.id$")
    _EMBEDDING_STATE_READ = re.compile(
        r"^MATCH \(n:(\w+)\) WHERE n\.id IN \$ids RETURN n\.id, n\.embedding IS NULL$"
    )
    _ANY_LABEL_READ = re.compile(
        r"^UNWIND \$ids AS id MATCH \(n \{id: id\}\) "
        r"WHERE (NOT n:\w+(?: AND NOT n:\w+)*) RETURN id$"
    )
    _EXCLUDED_LABEL = re.compile(r"NOT n:(\w+)")
    _MARKER_READ = re.compile(r"^MATCH \(m:Merged(\w+)\) RETURN m\.id, m\.merged_into$")
    _INDEX_READ = re.compile(r"^MATCH \(n:(\w+)\) RETURN n\.id, n\.(\w+), n\.embedding$")
    _DEFINED_INDEX_READ = re.compile(
        r"^MATCH \(:RegulatoryInstrument \{id: \$instrument_id\}\)-\[:DEFINES\]->"
        r"\(n:(\w+)\) RETURN n\.id, n\.(\w+), n\.embedding$"
    )

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Answer a read; a write statement is refused (every merge write is a gateway group)."""
        if _WRITE_CLAUSE.search(q):
            raise AssertionError(f"a write statement reached the read double: {q}")
        self.statements.append(q)
        self.params_seen.append(dict(params or {}))
        return _Result(self._read(q, params))

    def _read(self, q: str, params: dict[str, object] | None) -> list[object]:
        any_label = self._ANY_LABEL_READ.match(q)
        if any_label is not None:
            requested = cast("list[str]", (params or {})["ids"])
            excluded = set(self._EXCLUDED_LABEL.findall(any_label.group(1)))
            known = {node_id for label, node_id in self.nodes if label not in excluded}
            return [[node_id] for node_id in requested if node_id in known]
        marker_read = self._MARKER_READ.match(q)
        if marker_read is not None:
            return [
                [node_id, props.get("merged_into")]
                for (label, node_id), props in self.nodes.items()
                if label == f"Merged{marker_read.group(1)}"
            ]
        embedding_state = self._EMBEDDING_STATE_READ.match(q)
        if embedding_state is not None:
            requested = cast("list[str]", (params or {})["ids"])
            label = embedding_state.group(1)
            return [
                [i, self.nodes[(label, i)].get("embedding") is None]
                for i in requested
                if (label, i) in self.nodes
            ]
        existing = self._EXISTING_READ.match(q)
        if existing is not None:
            requested = cast("list[str]", (params or {})["ids"])
            return [[i] for i in requested if (existing.group(1), i) in self.nodes]
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
        defined_read = self._DEFINED_INDEX_READ.match(q)
        if defined_read is not None:
            label, text_key = defined_read.groups()
            instrument_id = cast("str", (params or {})["instrument_id"])
            return [
                [node_id, props.get(text_key), props.get("embedding")]
                for (node_label, node_id), props in self.nodes.items()
                if node_label == label and ("DEFINES", instrument_id, node_id) in self.edges
            ]
        if "MERGED_INTO" in q:
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
