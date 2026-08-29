"""Tests for `ps_service.company_merge.dedup.dedupe_canonical_nodes`
(PLAN_REVIEWED.md §10 Increment 9): the combined whole-collection
resolution algorithm -- exact match, semantic match/mint, in-run
convergence, and B2's within-run embedding-reuse fix.

Per the binding testing convention (PLAN_REVIEWED.md §0.5): `call_embedding`
is faked with a hand-written structural fake satisfying `EmbeddingCaller`
(mirrors `test_dedup_semantic_match.py`'s `_ScriptedCallEmbedding`); the
fake `single_tenant_graph` is a hand-written structural fake satisfying
`GraphHandle`, answering the one read query `read_existing_canonical_index`
issues and recording every call it receives (mirrors
`test_dedup_exact_match.py`'s `_ScriptedFakeGraph`).

Near-miss surfacing (test (c)) and the abort-on-embedding-failure/zero-write
property (test (e)) each have their own dedicated test file
(`test_dedup_near_miss.py`/`test_dedup_abort_on_embedding_failure.py`); the
false-convergence negative test (test (g), Q1's fix) has its own file too
(`test_dedup_false_convergence.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter

from collections import Counter

from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.dedup import dedupe_canonical_nodes
from ps_service.company_merge.models import BaselineNode

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
    `read_existing_canonical_index`'s one read query with pre-seeded
    Obligation rows, and records every `query()` call it receives -- used
    to assert `dedupe_canonical_nodes` never issues a write call.
    """

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


def _obligation(node_id: str, text: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": text, "confidence": 0.9})


def test_dedupe_canonical_nodes_exact_match_and_first_time_mint(
    make_emitter: MakeEmitter,
) -> None:
    """(a): two incoming nodes, one exact-matches an existing node, the
    other is a first-time mint -> correct DedupResult, exactly one
    match_kind="new".
    """
    emitter, _log_path = make_emitter()
    existing_id = "obl_existing_report_incident"
    new_id = "obl_new_never_seen_before"
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Report the incident.", [1.0, 0.0]]]
    )
    call_embedding = _ScriptedCallEmbedding({"A brand new duty never seen before.": [0.0, 1.0]})
    incoming_nodes = (
        _obligation(existing_id, "Report the incident."),
        _obligation(new_id, "A brand new duty never seen before."),
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

    assert len(result.resolutions) == 2
    exact = next(r for r in result.resolutions if r.incoming_id == existing_id)
    minted = next(r for r in result.resolutions if r.incoming_id == new_id)
    assert exact.match_kind == "exact"
    assert exact.canonical_id == existing_id
    assert minted.match_kind == "new"
    assert minted.canonical_id == new_id
    assert sum(1 for r in result.resolutions if r.match_kind == "new") == 1


def test_dedupe_canonical_nodes_semantic_match_resolves_onto_existing_id(
    make_emitter: MakeEmitter,
) -> None:
    """(b): one incoming node's best semantic score >= threshold ->
    match_kind="semantic", canonical_id is the EXISTING node's id, not its
    own.
    """
    emitter, _log_path = make_emitter()
    existing_id = "obl_existing_conduct_risk_assessment"
    incoming_id = "obl_incoming_perform_risk_assessment"
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Conduct a risk assessment.", [1.0, 0.0]]]
    )
    call_embedding = _ScriptedCallEmbedding(
        {"Perform a cybersecurity risk assessment.": [1.0, 0.0]}
    )
    incoming_nodes = (_obligation(incoming_id, "Perform a cybersecurity risk assessment."),)

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
    assert resolution.incoming_id == incoming_id
    assert resolution.match_kind == "semantic"
    assert resolution.canonical_id == existing_id
    assert resolution.canonical_id != incoming_id


def test_dedupe_canonical_nodes_in_run_convergence_onto_first_incoming_node(
    make_emitter: MakeEmitter,
) -> None:
    """(d): two incoming nodes with no existing match but semantically
    equivalent to EACH OTHER (mocked identical embeddings) -> the second
    resolves onto the first's id, not a separate mint.
    """
    emitter, _log_path = make_emitter()
    first_id = "obl_incoming_first_alpha"
    second_id = "obl_incoming_second_beta"
    graph = _ScriptedSingleTenantGraph(capability_rows=[])
    call_embedding = _ScriptedCallEmbedding(
        {
            "Notify the authority within 72 hours.": [1.0, 0.0],
            "Alert the authority within 72 hours.": [1.0, 0.0],
        }
    )
    incoming_nodes = (
        _obligation(first_id, "Notify the authority within 72 hours."),
        _obligation(second_id, "Alert the authority within 72 hours."),
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

    assert len(result.resolutions) == 2
    first_resolution = next(r for r in result.resolutions if r.incoming_id == first_id)
    second_resolution = next(r for r in result.resolutions if r.incoming_id == second_id)
    assert first_resolution.match_kind == "new"
    assert first_resolution.canonical_id == first_id
    assert second_resolution.match_kind == "semantic"
    assert second_resolution.canonical_id == first_id
    # first_id was minted THIS run, never present in the original existing
    # index -- its freshly-computed embedding is never a backfill candidate.
    assert result.embedding_backfills == {}


def test_dedupe_canonical_nodes_within_run_reuse_bounds_existing_side_cost(
    make_emitter: MakeEmitter,
) -> None:
    """(f), B2's within-run reuse integration proof: 3 existing entries with
    no cached embeddings, and 2 incoming nodes that both end up comparing
    against those same 3 entries -> the total call_embedding invocation
    count attributable to the existing side is exactly 3 (once each), not
    6; DedupResult.embedding_backfills contains exactly those 3 existing
    ids, each mapped to the embedding find_best_semantic_match actually
    returned for it. All five vectors below (3 existing + 2 incoming) are
    mutually orthogonal standard basis vectors, guaranteeing every score is
    0.0 (well below threshold), so nothing ever converges here -- the test
    is purely about embedding-call cost and backfill bookkeeping.
    """
    emitter, _log_path = make_emitter()
    existing_texts = {
        "obl_existing_one": "Existing duty one.",
        "obl_existing_two": "Existing duty two.",
        "obl_existing_three": "Existing duty three.",
    }
    existing_vectors = {
        "Existing duty one.": [1.0, 0.0, 0.0, 0.0, 0.0],
        "Existing duty two.": [0.0, 1.0, 0.0, 0.0, 0.0],
        "Existing duty three.": [0.0, 0.0, 1.0, 0.0, 0.0],
    }
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[node_id, text, None] for node_id, text in existing_texts.items()]
    )
    incoming_vectors = {
        "Brand new duty alpha.": [0.0, 0.0, 0.0, 1.0, 0.0],
        "Brand new duty beta.": [0.0, 0.0, 0.0, 0.0, 1.0],
    }
    call_embedding = _ScriptedCallEmbedding({**existing_vectors, **incoming_vectors})
    incoming_nodes = (
        _obligation("obl_new_alpha", "Brand new duty alpha."),
        _obligation("obl_new_beta", "Brand new duty beta."),
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

    call_counts = Counter(call_embedding.calls)
    for text in existing_vectors:
        assert call_counts[text] == 1, (
            f"{text!r} should be embedded exactly once, not {call_counts[text]}"
        )
    total_existing_side_calls = sum(call_counts[text] for text in existing_vectors)
    assert total_existing_side_calls == 3

    assert set(result.embedding_backfills) == set(existing_texts)
    for node_id, text in existing_texts.items():
        assert result.embedding_backfills[node_id] == tuple(existing_vectors[text])

    # Both incoming nodes minted separately -- all five vectors above are
    # mutually orthogonal by construction, so nothing converges here.
    assert {r.match_kind for r in result.resolutions} == {"new"}
    assert len({r.canonical_id for r in result.resolutions}) == 2
