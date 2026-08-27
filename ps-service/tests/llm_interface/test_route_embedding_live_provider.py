"""AC-002/AC-003 live tests: RouteEmbedding against the real, configured LLM Provider."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from ps_service.llm_interface.embedding import route_embedding
from ps_service.llm_interface.models import EmbeddingResult
from ps_service.logging.emitter import EmitterConfig, LogEmitter

pytestmark = pytest.mark.llm_live

# Captured at module-import time (collection), before the autouse `_isolate_logging`
# fixture in `tests/conftest.py` runs `monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", ...)`
# for every test. Reading `os.environ["PS_LLMINTERFACE_EMBED_MODEL"]` inside the test body
# itself would KeyError once that fixture has run — the guard exists precisely to
# keep leaked `.env` values out of unrelated tests, and this live test's whole point
# is to use the real value, so it must be captured before the fixture strips it.
_LLM_INTERFACE_EMBED_MODEL = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")

# Verified empirically during planning (PLAN.md §0) for the configured embedding model
# (`azure/text-embedding-3-large`, no `dimensions` param passed — its native/full output size).
_EXPECTED_VECTOR_DIMENSIONALITY = 3072


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    """A real `LogEmitter` writing to a per-test tmp path — `route_embedding`'s `_log` call

    needs a live emitter (or a configured process default) or it raises
    `LoggingLifecycleError`; these tests don't assert on log content, only
    that `route_embedding` itself behaves correctly, so a throwaway emitter
    is enough.
    """
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


@pytest.mark.skipif(
    not _LLM_INTERFACE_EMBED_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_EMBED_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_route_embedding_returns_vector_of_expected_dimensionality_from_live_provider(
    emitter: LogEmitter,
) -> None:
    assert _LLM_INTERFACE_EMBED_MODEL is not None  # narrows type for mypy/ruff; skipif already guards this
    result = route_embedding(
        "The quick brown fox jumps over the lazy dog.",
        model=_LLM_INTERFACE_EMBED_MODEL,
        call_embedding=None,
        emitter=emitter,
    )

    assert isinstance(result, EmbeddingResult)
    assert len(result.vector) == _EXPECTED_VECTOR_DIMENSIONALITY
    assert all(isinstance(v, float) for v in result.vector)


@pytest.mark.skipif(
    not _LLM_INTERFACE_EMBED_MODEL,
    reason="requires .env sourced (PS_LLMINTERFACE_EMBED_MODEL, AZURE_API_KEY, AZURE_API_BASE)",
)
def test_route_embedding_returns_identical_vector_for_identical_input_from_live_provider(
    emitter: LogEmitter,
) -> None:
    assert _LLM_INTERFACE_EMBED_MODEL is not None  # narrows type for mypy/ruff; skipif already guards this
    text = "The quick brown fox jumps over the lazy dog."

    result1 = route_embedding(
        text,
        model=_LLM_INTERFACE_EMBED_MODEL,
        call_embedding=None,
        emitter=emitter,
    )
    result2 = route_embedding(
        text,
        model=_LLM_INTERFACE_EMBED_MODEL,
        call_embedding=None,
        emitter=emitter,
    )

    assert result1.vector == result2.vector
