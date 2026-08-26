"""Tests for `ps_service.company_merge.dedup.find_best_semantic_match`
(PLAN_REVIEWED.md §10 Increment 8, B2's fix).

Per the binding testing convention (PLAN_REVIEWED.md §0.5): `call_embedding`
is faked with a hand-written structural fake satisfying `EmbeddingCaller`'s
Protocol (`llm_interface/client.py`), never `unittest.mock`/`MagicMock` --
mirrors `tests/llm_interface/test_route_embedding_mocked.py`'s and
`tests/domain_mapper/test_extraction.py`'s exact style.
"""

from __future__ import annotations

import httpx
import openai
import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.dedup import find_best_semantic_match
from ps_service.company_merge.models import ExistingCanonicalNode
from ps_service.company_merge.similarity import cosine_similarity
from ps_service.llm_interface.errors import LlmProviderError

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85


def _embedding_response(vector: list[float], *, model: str = _MODEL) -> EmbeddingResponse:
    return EmbeddingResponse(
        model=model,
        data=[Embedding(embedding=vector, index=0, object="embedding")],
    )


class _ScriptedCallEmbedding:
    """A hand-written `EmbeddingCaller` fake, scripted per input `text`
    (the sole item of the `input` list `route_embedding` always passes),
    keeping its own call log for assertion -- mirrors
    `test_extraction.py`'s `_scripted_call_completion` dispatch style, but
    keyed by exact text and logging every call it serves."""

    def __init__(self, vectors_by_text: dict[str, list[float] | Exception]) -> None:
        self._vectors_by_text = vectors_by_text
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
        return _embedding_response(scripted, model=model)

    def add_script(self, text: str, vector: list[float]) -> None:
        """Script a further response after construction -- used to extend a
        fake mid-test for a second `find_best_semantic_match` call."""
        self._vectors_by_text[text] = vector


def test_find_best_semantic_match_scores_all_entries_and_returns_the_max(make_emitter) -> None:
    """One call for the incoming text, one per existing entry missing a
    cached embedding -- correct best_existing_id/best_similarity via
    cosine_similarity."""
    emitter, _log_path = make_emitter()
    incoming_text = "Conduct a cybersecurity risk assessment."
    existing_index = (
        ExistingCanonicalNode(id="obligation_1", text="Report an incident.", embedding=None),
        ExistingCanonicalNode(
            id="obligation_2", text="Conduct a risk assessment.", embedding=None
        ),
    )
    incoming_vector = [1.0, 0.0, 0.0]
    obligation_1_vector = [0.0, 1.0, 0.0]
    obligation_2_vector = [0.9, 0.1, 0.0]
    call_embedding = _ScriptedCallEmbedding(
        {
            incoming_text: incoming_vector,
            "Report an incident.": obligation_1_vector,
            "Conduct a risk assessment.": obligation_2_vector,
        }
    )

    result = find_best_semantic_match(
        incoming_text,
        existing_index,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert result is not None
    expected_similarity = cosine_similarity(
        tuple(incoming_vector), tuple(obligation_2_vector)
    )
    assert result.best_existing_id == "obligation_2"
    assert result.best_similarity == expected_similarity
    assert result.incoming_embedding == tuple(incoming_vector)
    assert set(call_embedding.calls) == {
        incoming_text,
        "Report an incident.",
        "Conduct a risk assessment.",
    }


def test_find_best_semantic_match_reuses_cached_embedding_with_zero_calls_for_it(
    make_emitter,
) -> None:
    """An existing entry that already carries a cached `embedding` triggers
    ZERO `call_embedding` calls for it."""
    emitter, _log_path = make_emitter()
    incoming_text = "Conduct a cybersecurity risk assessment."
    cached_embedding = (0.9, 0.1, 0.0)
    existing_index = (
        ExistingCanonicalNode(
            id="obligation_1", text="Conduct a risk assessment.", embedding=cached_embedding
        ),
    )
    incoming_vector = [1.0, 0.0, 0.0]
    call_embedding = _ScriptedCallEmbedding({incoming_text: incoming_vector})

    result = find_best_semantic_match(
        incoming_text,
        existing_index,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert result is not None
    assert result.best_existing_id == "obligation_1"
    assert result.best_similarity == cosine_similarity(tuple(incoming_vector), cached_embedding)
    # Only the incoming text was ever embedded -- the cached existing
    # embedding was reused as-is, with no call for it.
    assert call_embedding.calls == [incoming_text]
    assert result.newly_computed_existing_embeddings == {}


def test_find_best_semantic_match_returns_none_and_makes_zero_calls_for_empty_index() -> None:
    """Empty `existing_index` -> `None`, and ZERO `call_embedding` calls at
    all -- not even for the incoming text."""
    call_embedding = _ScriptedCallEmbedding({})

    result = find_best_semantic_match(
        "Conduct a cybersecurity risk assessment.",
        (),
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
    )

    assert result is None
    assert call_embedding.calls == []


def test_find_best_semantic_match_propagates_llm_provider_error_unchanged(make_emitter) -> None:
    """A `call_embedding` raising `openai.OpenAIError` (mirroring how
    `route_embedding` wraps a real provider failure) propagates as
    `LlmProviderError`, unchanged, with no try/except swallowing it here."""
    emitter, _log_path = make_emitter()
    incoming_text = "Conduct a cybersecurity risk assessment."
    existing_index = (
        ExistingCanonicalNode(id="obligation_1", text="Report an incident.", embedding=None),
    )
    call_embedding = _ScriptedCallEmbedding(
        {
            incoming_text: [1.0, 0.0, 0.0],
            "Report an incident.": openai.APIConnectionError(
                request=httpx.Request("POST", "https://example.invalid")
            ),
        }
    )

    with pytest.raises(LlmProviderError):
        find_best_semantic_match(
            incoming_text,
            existing_index,
            model=_MODEL,
            threshold=_THRESHOLD,
            call_embedding=call_embedding,
            emitter=emitter,
        )


def test_find_best_semantic_match_newly_computed_embeddings_excludes_cached_entries(
    make_emitter,
) -> None:
    """B2 proof: `newly_computed_existing_embeddings` contains EXACTLY the
    ids that had no cached embedding going in -- never an id that already
    had one."""
    emitter, _log_path = make_emitter()
    incoming_text = "Conduct a cybersecurity risk assessment."
    cached_embedding = (0.5, 0.5, 0.0)
    existing_index = (
        ExistingCanonicalNode(
            id="obligation_cached", text="Already cached duty.", embedding=cached_embedding
        ),
        ExistingCanonicalNode(
            id="obligation_uncached_a", text="Report an incident.", embedding=None
        ),
        ExistingCanonicalNode(
            id="obligation_uncached_b", text="Maintain technical documentation.", embedding=None
        ),
    )
    incoming_vector = [1.0, 0.0, 0.0]
    uncached_a_vector = [0.2, 0.8, 0.0]
    uncached_b_vector = [0.1, 0.1, 0.9]
    call_embedding = _ScriptedCallEmbedding(
        {
            incoming_text: incoming_vector,
            "Report an incident.": uncached_a_vector,
            "Maintain technical documentation.": uncached_b_vector,
        }
    )

    result = find_best_semantic_match(
        incoming_text,
        existing_index,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert result is not None
    assert set(result.newly_computed_existing_embeddings) == {
        "obligation_uncached_a",
        "obligation_uncached_b",
    }
    assert "obligation_cached" not in result.newly_computed_existing_embeddings
    assert result.newly_computed_existing_embeddings["obligation_uncached_a"] == tuple(
        uncached_a_vector
    )
    assert result.newly_computed_existing_embeddings["obligation_uncached_b"] == tuple(
        uncached_b_vector
    )
    # Cached entry's own reuse cost zero calls; only the incoming text plus
    # the two genuinely-uncached entries were embedded.
    assert set(call_embedding.calls) == {
        incoming_text,
        "Report an incident.",
        "Maintain technical documentation.",
    }


def test_find_best_semantic_match_second_call_folds_in_first_calls_computed_embedding(
    make_emitter,
) -> None:
    """B2 proof (the primitive-level fold-in proof): calling
    `find_best_semantic_match` TWICE in direct sequence -- first against an
    `existing_index` with one entry's `embedding=None` (gets computed,
    appears in `newly_computed_existing_embeddings`); the test then manually
    builds a second `existing_index` tuple with that entry's `embedding`
    field replaced by the value the first call returned (simulating exactly
    what `dedupe_canonical_nodes` will do automatically in the next
    increment) -- and asserts the second call makes ZERO further
    `call_embedding` calls for that entry. This is the critical proof that
    `SemanticMatchResult`'s return-value contract actually holds."""
    emitter, _log_path = make_emitter()
    incoming_text_first = "Conduct a cybersecurity risk assessment."
    incoming_text_second = "Perform a cybersecurity risk assessment."
    existing_entry_text = "Report an incident."
    first_existing_index = (
        ExistingCanonicalNode(id="obligation_1", text=existing_entry_text, embedding=None),
    )
    first_incoming_vector = [1.0, 0.0, 0.0]
    computed_existing_vector = [0.2, 0.8, 0.0]
    call_embedding = _ScriptedCallEmbedding(
        {
            incoming_text_first: first_incoming_vector,
            existing_entry_text: computed_existing_vector,
        }
    )

    first_result = find_best_semantic_match(
        incoming_text_first,
        first_existing_index,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert first_result is not None
    assert first_result.newly_computed_existing_embeddings == {
        "obligation_1": tuple(computed_existing_vector)
    }
    assert call_embedding.calls == [incoming_text_first, existing_entry_text]

    # Simulate dedupe_canonical_nodes folding the freshly-computed embedding
    # into its working index before the next incoming node is processed.
    folded_embedding = first_result.newly_computed_existing_embeddings["obligation_1"]
    second_existing_index = (
        ExistingCanonicalNode(
            id="obligation_1", text=existing_entry_text, embedding=folded_embedding
        ),
    )
    second_incoming_vector = [0.95, 0.05, 0.0]
    call_embedding.add_script(incoming_text_second, second_incoming_vector)

    second_result = find_best_semantic_match(
        incoming_text_second,
        second_existing_index,
        model=_MODEL,
        threshold=_THRESHOLD,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    assert second_result is not None
    assert second_result.newly_computed_existing_embeddings == {}
    # Only the second incoming text was newly embedded this call -- the
    # existing entry's embedding, folded in from the first call's result,
    # cost zero further calls.
    assert call_embedding.calls == [
        incoming_text_first,
        existing_entry_text,
        incoming_text_second,
    ]
