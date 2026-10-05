"""Tests for issue #31: caching the INCOMING side of Company Merge's
Capability embedding comparison, at the `dedup.dedupe_canonical_nodes`
level.

Mirrors `test_dedup_combined_resolution.py`'s own fakes/style exactly
(`_ScriptedSingleTenantGraph`/`_ScriptedCallEmbedding`) -- these tests only
add `BaselineNode.embedding` to the incoming side of that same picture,
proving `DedupResult.incoming_embedding_backfills` (AC-BI-005) is populated
(or not) exactly when `find_best_semantic_match` did (or didn't) need a
fresh `route_embedding` call for the incoming node's own text (AC-BI-002/
AC-BI-003/AC-BI-004).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter

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
    """Satisfies `GraphHandle` structurally -- copied from
    `test_dedup_combined_resolution.py`'s own fake exactly.
    """

    def __init__(self, *, capability_rows: list[object] | None = None) -> None:
        self._capability_rows = capability_rows if capability_rows is not None else []
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if "MERGED_INTO" in q:
            return _FakeQueryResult([])
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


def _capability(
    node_id: str, name: str, *, embedding: tuple[float, ...] | None = None
) -> BaselineNode:
    return BaselineNode(
        id=node_id, properties={"name": name, "confidence": 0.9}, embedding=embedding
    )


def test_uncached_incoming_capability_merging_semantically_records_one_backfill(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-002/AC-BI-004: an incoming Capability with `embedding=None`
    (never cached before) that resolves via a semantic merge costs exactly
    one `route_embedding` call for its own text, and that freshly-computed
    embedding is recorded into `incoming_embedding_backfills` -- for
    `merge.py` to persist onto this Capability's own `{short}_baseline` node
    (AC-BI-005), regardless of the merge/mint outcome.
    """
    emitter, _log_path = make_emitter()
    existing_id = "cap_existing_risk_assessment"
    incoming_id = "cap_incoming_risk_assessment"
    incoming_text = "Perform a cybersecurity risk assessment."
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Conduct a risk assessment.", [1.0, 0.0]]]
    )
    call_embedding = _ScriptedCallEmbedding({incoming_text: [1.0, 0.0]})
    incoming_nodes = (_capability(incoming_id, incoming_text, embedding=None),)

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind="Capability",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    resolution = result.resolutions[0]
    assert resolution.match_kind == "semantic"
    assert resolution.canonical_id == existing_id
    assert call_embedding.calls == [incoming_text]
    assert result.incoming_embedding_backfills == {incoming_id: (1.0, 0.0)}


def test_cached_incoming_capability_costs_zero_calls_and_records_no_backfill(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-003: an incoming Capability whose `BaselineNode.embedding` is
    already cached (e.g. from a prior run's backfill) is reused directly --
    ZERO `route_embedding` calls for it -- and, since nothing new was
    computed, `incoming_embedding_backfills` carries no entry for it either
    (nothing to write that isn't already there).
    """
    emitter, _log_path = make_emitter()
    existing_id = "cap_existing_risk_assessment"
    incoming_id = "cap_incoming_risk_assessment"
    cached_embedding = (1.0, 0.0)
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Conduct a risk assessment.", [1.0, 0.0]]]
    )
    # No script for the incoming text at all -- proves it is never even
    # attempted, not merely that its result would be discarded.
    call_embedding = _ScriptedCallEmbedding({})
    incoming_nodes = (
        _capability(
            incoming_id,
            "Perform a cybersecurity risk assessment.",
            embedding=cached_embedding,
        ),
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

    resolution = result.resolutions[0]
    assert resolution.match_kind == "semantic"
    assert resolution.canonical_id == existing_id
    assert call_embedding.calls == []
    assert result.incoming_embedding_backfills == {}


def test_uncached_incoming_capability_minting_still_records_backfill(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-005: the backfill is recorded whenever a fresh embedding was
    actually computed, independent of whether the node goes on to mint or
    merge -- here the incoming node scores below `_THRESHOLD` against the
    one pre-existing entry, so it mints (`match_kind="new"`), but its own
    freshly-computed embedding is still captured for backfill.
    """
    emitter, _log_path = make_emitter()
    existing_id = "cap_existing_unrelated"
    incoming_id = "cap_incoming_new_concept"
    incoming_text = "Perform quantum-safe key rotation."
    graph = _ScriptedSingleTenantGraph(
        capability_rows=[[existing_id, "Conduct a risk assessment.", [1.0, 0.0]]]
    )
    call_embedding = _ScriptedCallEmbedding({incoming_text: [0.0, 1.0]})
    incoming_nodes = (_capability(incoming_id, incoming_text, embedding=None),)

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind="Capability",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    resolution = result.resolutions[0]
    assert resolution.match_kind == "new"
    assert resolution.canonical_id == incoming_id
    assert result.incoming_embedding_backfills == {incoming_id: (0.0, 1.0)}


def test_empty_existing_index_records_no_incoming_backfill(make_emitter: MakeEmitter) -> None:
    """No pre-existing Capability at all: `find_best_semantic_match` returns
    `None` without computing anything (its own pre-#31 contract, unchanged),
    so there is nothing to backfill either -- an uncached incoming node's
    `embedding` stays `None` at mint time, exactly as before #31, and zero
    `route_embedding` calls of any kind are made.
    """
    emitter, _log_path = make_emitter()
    incoming_id = "cap_incoming_first_ever"
    graph = _ScriptedSingleTenantGraph(capability_rows=[])
    call_embedding = _ScriptedCallEmbedding({})
    incoming_nodes = (_capability(incoming_id, "Some brand new capability.", embedding=None),)

    result = dedupe_canonical_nodes(
        incoming_nodes,
        kind="Capability",
        single_tenant_graph=graph,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    resolution = result.resolutions[0]
    assert resolution.match_kind == "new"
    assert resolution.embedding is None
    assert call_embedding.calls == []
    assert result.incoming_embedding_backfills == {}
