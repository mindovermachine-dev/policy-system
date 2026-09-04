"""Tests for `ps_service.export.export_instrument.export_instrument`
(PLAN.md Slice 3.5, full orchestration, CHANGES2.md §3.9's amended
fake-collaborator shape).

Every collaborator here is a fake `_GraphQueryHandle` (CHANGES2.md §3.9) --
no real FalkorDB/LLM call in this slice (that proof belongs to
`test_serialize_live.py`/`test_serialize_roundtrip_live.py` and Slice 3.6's
`test_engineering_practices_migration_live.py`). `_FakeInstrumentGraph`
dispatches by exact query text, mirroring `tests/export/test_serialize.py`'s
`_ScriptedFakeGraphQueryHandle`/`tests/export/test_embeddings.py`'s
`_FakeBaselineGraph` conventions, extended to answer BOTH
`embeddings.backfill_capability_embeddings`'s and `serialize.
serialize_graph`'s query shapes against the same handle -- exactly what
`export_instrument` passes the same `baseline_graph` object to both of, in
that order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.export.export_instrument import InstrumentDescriptor, export_instrument
from ps_service.export.models import InstrumentManifest

if TYPE_CHECKING:
    from pathlib import Path

    from company_merge._fakes import MakeEmitter

_EMBEDDING_TEXT_PROPERTY_BY_LABEL = {"Capability": "name", "Policy": "title"}

_CRA_DESCRIPTOR = InstrumentDescriptor(
    short_name="CRA",
    instrument_id="CRA-1.0",
    version="1.0",
    celex="32024R2847",
    title="Cyber Resilience Act",
    source_type="external",
    jurisdiction="EU",
)

_ENGPRAC_DESCRIPTOR = InstrumentDescriptor(
    short_name="ENGPRAC",
    instrument_id="ENGPRAC-2.1",
    version="2.1",
    celex=None,
    title="Engineering Practices Standard",
    source_type="internal",
    jurisdiction=None,
)


@dataclass
class _FakeNode:
    label: str
    properties: dict[str, object]


@dataclass
class _FakeEdge:
    relationship_type: str
    source_label: str
    source_id: str
    target_label: str
    target_id: str
    properties: dict[str, object]


class _FakeQueryResult:
    def __init__(self, result_set: list[list[object]]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[list[object]]:
        return self._result_set


@dataclass
class _FakeInstrumentGraph:
    """Satisfies `_GraphQueryHandle` for both embeddings backfill and serialize."""

    name: str
    nodes: list[_FakeNode]
    edges: list[_FakeEdge] = field(default_factory=list)
    call_log: list[str] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.call_log.append(f"{self.name}:{q}")
        result = self._dispatch(q, params)
        if result is None:
            raise AssertionError(f"unexpected query for graph {self.name!r}: {q!r}")
        return result

    def _dispatch(self, q: str, params: dict[str, object] | None) -> _FakeQueryResult | None:
        labels = sorted({node.label for node in self.nodes})
        relationship_types = sorted({edge.relationship_type for edge in self.edges})

        if q == "CALL db.labels()":
            return _FakeQueryResult([[label] for label in labels])
        if q == "CALL db.relationshipTypes()":
            return _FakeQueryResult([[rel] for rel in relationship_types])
        if q == "MATCH (n) RETURN count(n)":
            return _FakeQueryResult([[len(self.nodes)]])

        for label in labels:
            if q == f"MATCH (n:{label}) RETURN labels(n), properties(n)":
                rows: list[list[object]] = [
                    [[node.label], dict(node.properties)]
                    for node in self.nodes
                    if node.label == label
                ]
                return _FakeQueryResult(rows)

        embedding_result = self._dispatch_embedding_query(q, params)
        if embedding_result is not None:
            return embedding_result

        for relationship_type in relationship_types:
            if f"[r:{relationship_type}]" in q:
                return _FakeQueryResult(
                    [
                        [
                            [edge.source_label],
                            edge.source_id,
                            [edge.target_label],
                            edge.target_id,
                            dict(edge.properties),
                        ]
                        for edge in self.edges
                        if edge.relationship_type == relationship_type
                    ]
                )
        return None

    def _dispatch_embedding_query(
        self, q: str, params: dict[str, object] | None
    ) -> _FakeQueryResult | None:
        # Embeddings backfill queries for a fixed set of labels (Capability/Policy)
        # regardless of whether this graph happens to hold any nodes of that label.
        for label, text_property in _EMBEDDING_TEXT_PROPERTY_BY_LABEL.items():
            if q == f"MATCH (n:{label}) WHERE n.embedding IS NULL RETURN n.id, n.{text_property}":
                rows = [
                    [node.properties["id"], node.properties[text_property]]
                    for node in self.nodes
                    if node.label == label and "embedding" not in node.properties
                ]
                return _FakeQueryResult(rows)
            if q == f"MATCH (n:{label} {{id: $id}}) SET n.embedding = $embedding":
                assert params is not None
                node = next(
                    n for n in self.nodes if n.label == label and n.properties["id"] == params["id"]
                )
                node.properties["embedding"] = params["embedding"]
                return _FakeQueryResult([])
        return None


class _FakeEmbeddingCaller:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        self.calls.append(inputs[0])
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=self._vector, index=0, object="embedding")]
        )


def _baseline_graph() -> _FakeInstrumentGraph:
    return _FakeInstrumentGraph(
        name="baseline",
        nodes=[
            _FakeNode(
                label="RegulatoryInstrument",
                properties={"id": "CRA-1.0", "title": "Cyber Resilience Act"},
            ),
            _FakeNode(
                label="Capability",
                properties={"id": "cap_1", "name": "Data Encryption", "confidence": 0.9},
            ),
        ],
    )


def _native_graph() -> _FakeInstrumentGraph:
    return _FakeInstrumentGraph(
        name="native",
        nodes=[
            _FakeNode(
                label="RegulatoryInstrument",
                properties={"id": "CRA-1.0", "title": "Cyber Resilience Act"},
            )
        ],
    )


def test_export_instrument_writes_manifest_and_both_graph_files(
    tmp_path: Path, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    manifest = export_instrument(
        _CRA_DESCRIPTOR,
        baseline_graph=_baseline_graph(),
        native_graph=_native_graph(),
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=_FakeEmbeddingCaller([0.1, 0.2]),
        emitter=emitter,
    )

    instrument_dir = repo_root / "curated-content" / "CRA-1.0"
    assert (instrument_dir / "manifest.json").is_file()
    assert (instrument_dir / "baseline.json").is_file()
    assert (instrument_dir / "native.json").is_file()
    assert manifest.instrument_id == "CRA-1.0"
    assert manifest.celex == "32024R2847"
    assert manifest.source_type == "external"
    assert manifest.jurisdiction == "EU"
    assert manifest.schema_version == "1"
    assert len(manifest.baseline_sha256) == 64
    assert len(manifest.native_sha256) == 64


def test_export_instrument_persisted_baseline_json_contains_the_backfilled_embedding(
    tmp_path: Path, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    export_instrument(
        _CRA_DESCRIPTOR,
        baseline_graph=_baseline_graph(),
        native_graph=_native_graph(),
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=_FakeEmbeddingCaller([0.1, 0.2]),
        emitter=emitter,
    )

    baseline_path = repo_root / "curated-content" / "CRA-1.0" / "baseline.json"
    baseline_document = json.loads(baseline_path.read_text(encoding="utf-8"))
    capability_nodes = [n for n in baseline_document["nodes"] if n["label"] == "Capability"]
    assert len(capability_nodes) == 1
    assert capability_nodes[0]["properties"]["embedding"] == [0.1, 0.2]


def test_export_instrument_updates_catalog_json_in_both_locations(
    tmp_path: Path, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    export_instrument(
        _CRA_DESCRIPTOR,
        baseline_graph=_baseline_graph(),
        native_graph=_native_graph(),
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=_FakeEmbeddingCaller([0.1, 0.2]),
        emitter=emitter,
    )

    repo_catalog = json.loads(
        (repo_root / "curated-content" / "catalog.json").read_text(encoding="utf-8")
    )
    packaged_catalog = json.loads(packaged_copy_path.read_text(encoding="utf-8"))
    assert repo_catalog == packaged_catalog
    assert repo_catalog == [
        {
            "instrument_id": "CRA-1.0",
            "celex": "32024R2847",
            "title": "Cyber Resilience Act",
            "source_type": "external",
            "jurisdiction": "EU",
            "short_name": "CRA",
            "version": "1.0",
        }
    ]


def test_export_instrument_second_export_preserves_the_first_instruments_catalog_entry(
    tmp_path: Path, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"
    export_instrument(
        _CRA_DESCRIPTOR,
        baseline_graph=_baseline_graph(),
        native_graph=_native_graph(),
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=_FakeEmbeddingCaller([0.1, 0.2]),
        emitter=emitter,
    )
    engprac_node = _FakeNode(
        label="RegulatoryInstrument",
        properties={"id": "ENGPRAC-2.1", "title": "Engineering Practices Standard"},
    )

    export_instrument(
        _ENGPRAC_DESCRIPTOR,
        baseline_graph=_FakeInstrumentGraph(name="engprac_baseline", nodes=[engprac_node]),
        native_graph=_FakeInstrumentGraph(name="engprac_native", nodes=[engprac_node]),
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=_FakeEmbeddingCaller([0.3]),
        emitter=emitter,
    )

    catalog = json.loads(
        (repo_root / "curated-content" / "catalog.json").read_text(encoding="utf-8")
    )
    assert [entry["instrument_id"] for entry in catalog] == ["CRA-1.0", "ENGPRAC-2.1"]


def test_export_instrument_backfills_embeddings_before_serializing_the_baseline_graph(
    tmp_path: Path, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"
    baseline_graph = _baseline_graph()
    embedding_caller = _FakeEmbeddingCaller([0.1, 0.2])

    export_instrument(
        _CRA_DESCRIPTOR,
        baseline_graph=baseline_graph,
        native_graph=_native_graph(),
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=embedding_caller,
        emitter=emitter,
    )

    embedding_write_index = next(
        i
        for i, entry in enumerate(baseline_graph.call_log)
        if "SET n.embedding = $embedding" in entry
    )
    serialize_start_index = next(
        i for i, entry in enumerate(baseline_graph.call_log) if entry.endswith("CALL db.labels()")
    )
    assert embedding_write_index < serialize_start_index
    # The embedding call itself happened before the write that used its result.
    assert embedding_caller.calls == ["Data Encryption"]


def test_export_instrument_returns_the_manifest_matching_the_written_file(
    tmp_path: Path, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    manifest = export_instrument(
        _CRA_DESCRIPTOR,
        baseline_graph=_baseline_graph(),
        native_graph=_native_graph(),
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=_FakeEmbeddingCaller([0.1, 0.2]),
        emitter=emitter,
    )

    instrument_dir = repo_root / "curated-content" / "CRA-1.0"
    document = json.loads((instrument_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == InstrumentManifest(**document)
