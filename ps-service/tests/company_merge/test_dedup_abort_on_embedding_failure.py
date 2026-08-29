"""Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`
(PLAN_REVIEWED.md §10 Increment 9, test (e)): a mocked embedding failure
partway through a 3-node collection propagates `LlmProviderError`
unchanged, and `dedupe_canonical_nodes` has made ZERO write calls against
`single_tenant_graph` by the time it aborts -- only the one read call from
`read_existing_canonical_index`. `dedupe_canonical_nodes` never issues a
write call at all, so "abort with no partial write" holds by construction;
this test is the direct proof of that call-log shape.
"""

from __future__ import annotations

import httpx
import openai
import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.models import BaselineNode
from ps_service.llm_interface.errors import LlmProviderError

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
    """Satisfies `GraphHandle` structurally: records every `query()` call
    it receives, so a test can assert the call log contains no write."""

    def __init__(self, *, capability_rows: list[object] | None = None) -> None:
        self._capability_rows = capability_rows if capability_rows is not None else []
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "(n:Capability) RETURN" in q:
            return _FakeQueryResult(self._capability_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text` --
    a scripted `Exception` value is raised instead of returning a response,
    mirroring `test_dedup_semantic_match.py`'s propagation test."""

    def __init__(self, vectors_by_text: dict[str, list[float] | Exception]) -> None:
        self._vectors_by_text = dict(vectors_by_text)
        self.calls: list[str] = []

    def __call__(self, *, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        assert len(input) == 1
        text = input[0]
        self.calls.append(text)
        scripted = self._vectors_by_text.get(text)
        if scripted is None:
            raise AssertionError(f"no scripted response for text: {text!r}")
        if isinstance(scripted, Exception):
            raise scripted
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=scripted, index=0, object="embedding")]
        )


def test_dedupe_canonical_nodes_aborts_with_zero_writes_on_embedding_failure(make_emitter) -> None:
    """3 incoming nodes; the second one's own embedding call raises. The
    exception propagates unchanged, the third node is never processed, and
    `single_tenant_graph`'s call log contains exactly the one read call
    `read_existing_canonical_index` issued at the very start -- zero write
    calls of any kind."""
    emitter, _log_path = make_emitter()
    graph = _ScriptedSingleTenantGraph(capability_rows=[])
    first_text = "First duty, minted with no comparison needed."
    second_text = "Second duty -- its own embedding call fails."
    third_text = "Third duty, never reached."
    call_embedding = _ScriptedCallEmbedding(
        {
            second_text: openai.APIConnectionError(
                request=httpx.Request("POST", "https://example.invalid")
            )
        }
    )
    incoming_nodes = (
        BaselineNode(id="obl_first", properties={"name": first_text, "confidence": 0.9}),
        BaselineNode(id="obl_second", properties={"name": second_text, "confidence": 0.9}),
        BaselineNode(id="obl_third", properties={"name": third_text, "confidence": 0.9}),
    )

    with pytest.raises(LlmProviderError):
        dedupe_canonical_nodes(
            incoming_nodes,
            kind="Capability",
            single_tenant_graph=graph,
            model=_MODEL,
            threshold=_THRESHOLD,
            call_embedding=call_embedding,
            emitter=emitter,
        )

    assert len(graph.calls) == 1
    assert "RETURN" in graph.calls[0]
    assert "MERGE" not in graph.calls[0]
    assert "SET" not in graph.calls[0]
    # The first node minted with zero embedding calls at all (empty
    # existing index short-circuits find_best_semantic_match); only the
    # second node's own incoming-text embedding call was ever attempted
    # before the exception propagated; the third node was never reached.
    assert call_embedding.calls == [second_text]
