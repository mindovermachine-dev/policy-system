"""Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes` with `kind="Policy"`
(issue #54, S4): Policy convergence via exact canonical-id match and via
semantic match -- the same combined whole-collection resolution algorithm
`test_dedup_combined_resolution.py` already proves for Capability, exercised
here for Policy instead, since `dedupe_canonical_nodes`'s `kind` parameter
now widens to `Literal["Capability", "Policy"]`.

Per the binding testing convention (PLAN_REVIEWED.md §0.5): `call_embedding`
is faked with a hand-written structural fake satisfying `EmbeddingCaller`
(mirrors `test_dedup_semantic_match.py`'s `_ScriptedCallEmbedding`); the
fake `single_tenant_graph` is a hand-written structural fake satisfying
`GraphHandle`, answering `read_existing_canonical_index`'s one read query
for `Policy` (`n.title`, not `n.name` -- Policy's own text property) and
recording every call it receives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter

from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.models import BaselineNode
from ps_service.domain_mapper.identity import policy_id

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
    """Satisfies `GraphHandle` structurally: answers
    `read_existing_canonical_index`'s Policy read query (`n.id, n.title,
    n.embedding`) with pre-seeded rows, and records every `query()` call it
    receives -- used to assert `dedupe_canonical_nodes` never issues a
    write call.
    """

    def __init__(self, *, policy_rows: list[object] | None = None) -> None:
        self._policy_rows = policy_rows if policy_rows is not None else []
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "(n:Policy) RETURN" in q:
            return _FakeQueryResult(self._policy_rows)
        raise AssertionError(f"unexpected query issued: {q!r}")


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text` --
    mirrors `test_dedup_semantic_match.py`'s own fake exactly.
    """

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


def _policy_node(node_id: str, title: str) -> BaselineNode:
    return BaselineNode(
        id=node_id, properties={"title": title, "status": "draft", "confidence": 0.9}
    )


def test_policy_converges_by_exact_canonical_id(make_emitter: MakeEmitter) -> None:
    """An incoming Policy whose id is already `policy_id(title)`-computed
    identically to an existing canonical Policy id resolves via exact-key
    match, onto that SAME id -- no semantic comparison, no embedding call.
    """
    emitter, _log_path = make_emitter()
    title = "Engineering Practices Policy"
    existing_id = policy_id(title)
    graph = _ScriptedSingleTenantGraph(policy_rows=[[existing_id, title, [1.0, 0.0]]])
    call_embedding = _ScriptedCallEmbedding({})  # must never be called
    incoming_nodes = (_policy_node(existing_id, title),)

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind="Policy",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.incoming_id == existing_id
    assert resolution.match_kind == "exact"
    assert resolution.canonical_id == existing_id
    assert call_embedding.calls == []
    assert not any("MERGE" in c for c in graph.calls), "dedup itself never writes"


def test_policy_converges_by_semantic_match(make_emitter: MakeEmitter) -> None:
    """An incoming Policy whose title differs from an existing canonical
    Policy's title (so its computed id differs too -- no exact-key match),
    but whose best semantic score is >= threshold, resolves via
    `match_kind="semantic"` onto the EXISTING Policy's canonical id -- not
    its own incoming id.
    """
    emitter, _log_path = make_emitter()
    existing_title = "Engineering Practices Policy"
    existing_id = policy_id(existing_title)
    incoming_title = "Engineering Practice Policy"  # reworded, distinct id
    incoming_id = policy_id(incoming_title)
    assert incoming_id != existing_id

    graph = _ScriptedSingleTenantGraph(policy_rows=[[existing_id, existing_title, [1.0, 0.0]]])
    call_embedding = _ScriptedCallEmbedding({incoming_title: [1.0, 0.0]})
    incoming_nodes = (_policy_node(incoming_id, incoming_title),)

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind="Policy",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.incoming_id == incoming_id
    assert resolution.match_kind == "semantic"
    assert resolution.canonical_id == existing_id
    assert resolution.canonical_id != incoming_id
