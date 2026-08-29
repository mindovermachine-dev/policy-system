"""Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`
(PLAN_REVIEWED.md §10 Increment 9, test (c)): AC-004 near-miss surfacing --
a below-threshold semantic score mints a new canonical node AND records a
`NearMissPair` carrying the actual similarity value that was computed.
"""

from __future__ import annotations

from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.models import BaselineNode
from ps_service.company_merge.similarity import cosine_similarity

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _ScriptedSingleTenantGraph:
    """Satisfies `GraphHandle` structurally."""

    def __init__(self, *, capability_rows: list[object] | None = None) -> None:
        self._capability_rows = capability_rows if capability_rows is not None else []
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text`."""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        assert len(input) == 1
        text = input[0]
        self.calls.append(text)
        vector = self._vectors_by_text.get(text)
        if vector is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def test_dedupe_canonical_nodes_below_threshold_mints_and_records_near_miss(make_emitter) -> None:
    emitter, _log_path = make_emitter()
    existing_id = "obl_existing_conduct_risk_assessment"
    incoming_id = "obl_incoming_report_incident"
    existing_text = "Conduct a risk assessment."
    incoming_text = "Report the incident to the authority."
    existing_vector = [1.0, 0.0]
    incoming_vector = [0.0, 1.0]
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, existing_text, existing_vector]]
    )
    call_embedding = _ScriptedCallEmbedding({incoming_text: incoming_vector})
    incoming_nodes = (
        BaselineNode(id=incoming_id, properties={"name": incoming_text, "confidence": 0.9}),
    )

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind="Capability",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.match_kind == "new"
    assert resolution.canonical_id == incoming_id

    expected_similarity = cosine_similarity(tuple(incoming_vector), tuple(existing_vector))
    assert expected_similarity < _THRESHOLD

    assert len(result.near_misses) == 1
    near_miss = result.near_misses[0]
    assert near_miss.incoming_id == incoming_id
    assert near_miss.incoming_text == incoming_text
    assert near_miss.nearest_existing_id == existing_id
    assert near_miss.nearest_existing_text == existing_text
    assert near_miss.similarity == expected_similarity
