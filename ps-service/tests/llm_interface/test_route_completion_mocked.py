"""Tests for ps_service.llm_interface.completion.route_completion (mocked provider calls)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import openai
import pytest
from litellm.types.utils import Choices, Message, ModelResponse
from ps_service.dependency_health import LLM_INTERFACE, is_healthy
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.llm_interface.models import ChatMessage, CompletionResult
from ps_service.logging.emitter import EmitterConfig, LogEmitter


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    """A real `LogEmitter` writing to a per-test tmp path — `route_completion`'s `_log` call

    needs a live emitter (or a configured process default) or it raises
    `LoggingLifecycleError`; these tests don't assert on log content, only
    that `route_completion` itself behaves correctly, so a throwaway emitter
    is enough.
    """
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


def test_route_completion_returns_completion_result_with_provider_text(emitter: LogEmitter) -> None:
    fake_response = ModelResponse(
        id="x",
        model="fake-model",
        choices=[Choices(finish_reason="stop", index=0, message=Message(content="hi there", role="assistant"))],
    )

    def fake_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        return fake_response

    result = route_completion(
        [ChatMessage(role="user", content="hi")],
        model="fake-model",
        call_completion=fake_call_completion,
        emitter=emitter,
    )

    assert isinstance(result, CompletionResult)
    assert result.text == "hi there"
    assert result.model == "fake-model"


def test_route_completion_raises_llm_provider_error_when_provider_call_raises(emitter: LogEmitter) -> None:
    def fake_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError) as exc_info:
        route_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            call_completion=fake_call_completion,
            emitter=emitter,
        )

    assert isinstance(exc_info.value.__cause__, openai.APIConnectionError)


def test_route_completion_raises_llm_provider_error_when_provider_returns_no_choices(
    emitter: LogEmitter,
) -> None:
    fake_response = ModelResponse(id="x", model="fake-model", choices=[])

    def fake_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        return fake_response

    with pytest.raises(LlmProviderError):
        route_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            call_completion=fake_call_completion,
            emitter=emitter,
        )


def test_route_completion_marks_llm_interface_healthy_on_success(emitter: LogEmitter) -> None:
    fake_response = ModelResponse(
        id="x",
        model="fake-model",
        choices=[Choices(finish_reason="stop", index=0, message=Message(content="hi there", role="assistant"))],
    )

    def fake_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        return fake_response

    route_completion(
        [ChatMessage(role="user", content="hi")],
        model="fake-model",
        call_completion=fake_call_completion,
        emitter=emitter,
    )

    assert is_healthy(LLM_INTERFACE) is True


def test_route_completion_marks_llm_interface_unhealthy_when_provider_call_raises(emitter: LogEmitter) -> None:
    def fake_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError):
        route_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            call_completion=fake_call_completion,
            emitter=emitter,
        )

    assert is_healthy(LLM_INTERFACE) is False


def test_route_completion_empty_choices_does_not_mark_llm_interface_unhealthy(emitter: LogEmitter) -> None:
    """An empty-choices response is a data-shape issue with this particular
    call, not evidence the provider is unreachable — must not be conflated
    with a transport-level failure."""
    fake_response = ModelResponse(id="x", model="fake-model", choices=[])

    def fake_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        return fake_response

    with pytest.raises(LlmProviderError):
        route_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            call_completion=fake_call_completion,
            emitter=emitter,
        )

    assert is_healthy(LLM_INTERFACE) is True


def test_route_completion_raises_llm_provider_error_when_provider_returns_empty_content(
    emitter: LogEmitter,
) -> None:
    fake_response = ModelResponse(
        id="x",
        model="fake-model",
        choices=[Choices(finish_reason="stop", index=0, message=Message(content="", role="assistant"))],
    )

    def fake_call_completion(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
        return fake_response

    with pytest.raises(LlmProviderError):
        route_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            call_completion=fake_call_completion,
            emitter=emitter,
        )
