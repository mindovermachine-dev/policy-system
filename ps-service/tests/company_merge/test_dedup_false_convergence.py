"""Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`
(PLAN_REVIEWED.md §10 Increment 9, test (g), Q1's fix): the false-convergence
negative test. Two incoming nodes with genuinely different duty text and a
mocked similarity score for their mutual comparison deliberately JUST BELOW
threshold must resolve as two SEPARATE match_kind="new" entries with
DISTINCT canonical_id values -- proving the in-run convergence mechanism
(§5.4 step 6c) does not mis-fire when the two nodes are not actually
equivalent. This closes the gap a positive convergence test alone
(test (d), `test_dedup_combined_resolution.py`) leaves open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter

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

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        vector = self._vectors_by_text.get(text)
        if vector is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def test_dedupe_canonical_nodes_below_threshold_pair_does_not_converge(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    first_id = "obl_notify_authority_breach"
    second_id = "obl_register_processing_activities"
    first_text = "Notify the data protection authority of a breach."
    second_text = "Maintain a register of processing activities."
    # Both unit vectors -- cosine similarity is their dot product exactly:
    # 0.8*1.0 + 0.6*0.0 = 0.8, deliberately just below _THRESHOLD (0.85).
    first_vector = [1.0, 0.0]
    second_vector = [0.8, 0.6]
    graph = _ScriptedSingleTenantGraph(capability_rows=[])
    call_embedding = _ScriptedCallEmbedding({first_text: first_vector, second_text: second_vector})
    incoming_nodes = (
        BaselineNode(id=first_id, properties={"name": first_text, "confidence": 0.9}),
        BaselineNode(id=second_id, properties={"name": second_text, "confidence": 0.9}),
    )

    similarity = cosine_similarity(tuple(second_vector), tuple(first_vector))
    assert similarity < _THRESHOLD

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind="Capability",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert len(result.resolutions) == 2
    first_resolution = next(r for r in result.resolutions if r.incoming_id == first_id)
    second_resolution = next(r for r in result.resolutions if r.incoming_id == second_id)
    assert first_resolution.match_kind == "new"
    assert second_resolution.match_kind == "new"
    assert first_resolution.canonical_id == first_id
    assert second_resolution.canonical_id == second_id
    assert first_resolution.canonical_id != second_resolution.canonical_id

    assert len(result.near_misses) == 1
    assert result.near_misses[0].incoming_id == second_id
    assert result.near_misses[0].nearest_existing_id == first_id
