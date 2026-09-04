"""Tests for `ps_service.export.embeddings.backfill_capability_embeddings`
(PLAN.md Slices 3.1/3.2, D7).

The fake `_GraphQueryHandle` is a small in-memory node store dispatching by
exact query text -- mirroring `tests/export/test_serialize.py`'s
`_ScriptedFakeGraphQueryHandle` convention -- rather than importing this
module's private query-string constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.export.embeddings import backfill_capability_embeddings

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter


@dataclass
class _FakeNode:
    label: str
    node_id: str
    text: str
    embedding: list[float] | None = None


class _FakeQueryResult:
    def __init__(self, result_set: list[list[object]]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[list[object]]:
        return self._result_set


@dataclass
class _RecordedWrite:
    label: str
    node_id: str
    embedding: list[float]


@dataclass
class _FakeBaselineGraph:
    """In-memory `_GraphQueryHandle` fake holding a fixed set of nodes."""

    nodes: list[_FakeNode]
    writes: list[_RecordedWrite] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        for label, text_property in (("Capability", "name"), ("Policy", "title")):
            if q == f"MATCH (n:{label}) WHERE n.embedding IS NULL RETURN n.id, n.{text_property}":
                rows: list[list[object]] = [
                    [node.node_id, node.text]
                    for node in self.nodes
                    if node.label == label and node.embedding is None
                ]
                return _FakeQueryResult(rows)
            if q == f"MATCH (n:{label} {{id: $id}}) SET n.embedding = $embedding":
                assert params is not None
                node_id = params["id"]
                raw_embedding = params["embedding"]
                assert isinstance(node_id, str)
                assert isinstance(raw_embedding, list)
                embedding: list[float] = [
                    cast("float", value) for value in cast("list[object]", raw_embedding)
                ]
                node = next(n for n in self.nodes if n.label == label and n.node_id == node_id)
                node.embedding = embedding
                self.writes.append(
                    _RecordedWrite(label=label, node_id=node_id, embedding=embedding)
                )
                return _FakeQueryResult([])
        raise AssertionError(f"unexpected query: {q!r}")


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


def test_backfill_writes_one_embedding_per_capability_node_missing_one(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(
        nodes=[
            _FakeNode(label="Capability", node_id="cap_1", text="Data Encryption"),
            _FakeNode(label="Capability", node_id="cap_2", text="Access Control"),
        ]
    )
    caller = _FakeEmbeddingCaller([0.1, 0.2, 0.3])

    written = backfill_capability_embeddings(
        graph,
        source_type="external",
        model="fake-embed-model",
        call_embedding=caller,
        emitter=emitter,
    )

    assert written == 2
    assert sorted(caller.calls) == ["Access Control", "Data Encryption"]
    assert len(graph.writes) == 2
    assert {write.node_id for write in graph.writes} == {"cap_1", "cap_2"}
    for write in graph.writes:
        assert write.embedding == [0.1, 0.2, 0.3]
    for node in graph.nodes:
        assert node.embedding == [0.1, 0.2, 0.3]


def test_backfill_skips_capability_nodes_that_already_have_an_embedding(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(
        nodes=[
            _FakeNode(label="Capability", node_id="cap_1", text="Data Encryption", embedding=[0.9]),
            _FakeNode(label="Capability", node_id="cap_2", text="Access Control"),
        ]
    )
    caller = _FakeEmbeddingCaller([0.4, 0.5])

    written = backfill_capability_embeddings(
        graph,
        source_type="external",
        model="fake-embed-model",
        call_embedding=caller,
        emitter=emitter,
    )

    assert written == 1
    assert caller.calls == ["Access Control"]
    assert graph.nodes[0].embedding == [0.9]  # untouched
    assert graph.nodes[1].embedding == [0.4, 0.5]


def test_backfill_external_source_never_reads_or_writes_policy_nodes(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(
        nodes=[
            _FakeNode(label="Capability", node_id="cap_1", text="Data Encryption"),
            _FakeNode(label="Policy", node_id="pol_1", text="Data Protection Policy"),
        ]
    )
    caller = _FakeEmbeddingCaller([0.1])

    written = backfill_capability_embeddings(
        graph,
        source_type="external",
        model="fake-embed-model",
        call_embedding=caller,
        emitter=emitter,
    )

    assert written == 1
    assert caller.calls == ["Data Encryption"]
    policy_node = next(n for n in graph.nodes if n.label == "Policy")
    assert policy_node.embedding is None


def test_backfill_internal_source_also_covers_policy_nodes(make_emitter: MakeEmitter) -> None:
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(
        nodes=[
            _FakeNode(label="Capability", node_id="cap_1", text="Data Encryption"),
            _FakeNode(label="Policy", node_id="pol_1", text="Data Protection Policy"),
        ]
    )
    caller = _FakeEmbeddingCaller([0.7, 0.8])

    written = backfill_capability_embeddings(
        graph,
        source_type="internal",
        model="fake-embed-model",
        call_embedding=caller,
        emitter=emitter,
    )

    assert written == 2
    assert sorted(caller.calls) == ["Data Encryption", "Data Protection Policy"]
    policy_node = next(n for n in graph.nodes if n.label == "Policy")
    assert policy_node.embedding == [0.7, 0.8]


def test_backfill_with_no_missing_embeddings_writes_nothing(make_emitter: MakeEmitter) -> None:
    emitter, _log_path = make_emitter()
    graph = _FakeBaselineGraph(nodes=[])
    caller = _FakeEmbeddingCaller([0.1])

    written = backfill_capability_embeddings(
        graph,
        source_type="external",
        model="fake-embed-model",
        call_embedding=caller,
        emitter=emitter,
    )

    assert written == 0
    assert caller.calls == []
    assert graph.writes == []
