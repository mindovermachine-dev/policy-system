"""Tests for ps_service.llm_interface.embedding.route_embedding (mocked provider calls)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import openai
import pytest
from litellm.types.utils import Embedding, EmbeddingResponse
from ps_service.dependency_health import LLM_INTERFACE, is_healthy
from ps_service.llm_interface.embedding import route_embedding
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.llm_interface.models import EmbeddingResult
from ps_service.logging.emitter import EmitterConfig, LogEmitter


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


def test_route_embedding_returns_embedding_result_with_provider_vector(emitter: LogEmitter) -> None:
    fake_response = EmbeddingResponse(
        model="fake-embed-model",
        data=[Embedding(embedding=[0.1, 0.2, 0.3], index=0, object="embedding")],
    )

    def fake_call_embedding(*, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        return fake_response

    result = route_embedding(
        "some text",
        model="fake-embed-model",
        call_embedding=fake_call_embedding,
        emitter=emitter,
    )

    assert isinstance(result, EmbeddingResult)
    assert result.vector == [0.1, 0.2, 0.3]
    assert result.model == "fake-embed-model"


def test_route_embedding_raises_llm_provider_error_when_provider_call_raises(emitter: LogEmitter) -> None:
    def fake_call_embedding(*, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError) as exc_info:
        route_embedding(
            "some text", model="fake-embed-model", call_embedding=fake_call_embedding, emitter=emitter
        )

    assert isinstance(exc_info.value.__cause__, openai.APIConnectionError)


def test_route_embedding_raises_llm_provider_error_when_provider_returns_no_data(emitter: LogEmitter) -> None:
    fake_response = EmbeddingResponse(model="fake-embed-model", data=[])

    def fake_call_embedding(*, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        return fake_response

    with pytest.raises(LlmProviderError):
        route_embedding(
            "some text", model="fake-embed-model", call_embedding=fake_call_embedding, emitter=emitter
        )


def test_route_embedding_marks_llm_interface_healthy_on_success(emitter: LogEmitter) -> None:
    fake_response = EmbeddingResponse(
        model="fake-embed-model",
        data=[Embedding(embedding=[0.1, 0.2, 0.3], index=0, object="embedding")],
    )

    def fake_call_embedding(*, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        return fake_response

    route_embedding("some text", model="fake-embed-model", call_embedding=fake_call_embedding, emitter=emitter)

    assert is_healthy(LLM_INTERFACE) is True


def test_route_embedding_marks_llm_interface_unhealthy_when_provider_call_raises(emitter: LogEmitter) -> None:
    def fake_call_embedding(*, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError):
        route_embedding(
            "some text", model="fake-embed-model", call_embedding=fake_call_embedding, emitter=emitter
        )

    assert is_healthy(LLM_INTERFACE) is False


def test_route_embedding_empty_data_does_not_mark_llm_interface_unhealthy(emitter: LogEmitter) -> None:
    fake_response = EmbeddingResponse(model="fake-embed-model", data=[])

    def fake_call_embedding(*, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
        return fake_response

    with pytest.raises(LlmProviderError):
        route_embedding(
            "some text", model="fake-embed-model", call_embedding=fake_call_embedding, emitter=emitter
        )

    assert is_healthy(LLM_INTERFACE) is True
