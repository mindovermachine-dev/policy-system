"""Tests for ps_service.llm_interface.structured_completion.route_structured_completion.

Slice 1 (happy-path-only) tests plus Slice 2 (fail-fast on an unsupported model,
and the schema-mismatch error path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, assert_type

import httpx
import openai
import pydantic
import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from ps_service.dependency_health import LLM_INTERFACE, is_healthy, mark_unhealthy
from ps_service.llm_interface.errors import LlmProviderError, LlmResponseSchemaError
from ps_service.llm_interface.models import ChatMessage, StructuredCompletionResult
from ps_service.llm_interface.structured_completion import route_structured_completion

if TYPE_CHECKING:
    from conftest import MakeEmitter, ReadLines

    from ps_service.logging.emitter import LogEmitter


def _set_schema_support(monkeypatch: pytest.MonkeyPatch, *, supported: bool) -> None:
    def _fake_supports_response_schema(model: str, custom_llm_provider: str | None = None) -> bool:
        return supported

    monkeypatch.setattr(
        "ps_service.llm_interface.structured_completion.supports_response_schema",
        _fake_supports_response_schema,
    )


class _Extraction(pydantic.BaseModel):
    """A minimal response model used only by these tests."""

    label: str
    score: int


class _RecordingStructuredCaller:
    """Records every invocation's kwargs and returns a fixed `ModelResponse`."""

    def __init__(self, response: ModelResponse) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float,
        response_format: type[pydantic.BaseModel],
    ) -> ModelResponse:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "timeout": timeout,
                "response_format": response_format,
            }
        )
        return self._response


def _fake_response(*, model: str, content: str) -> ModelResponse:
    return ModelResponse(
        id="x",
        model=model,
        choices=[
            Choices(
                finish_reason="stop", index=0, message=Message(content=content, role="assistant")
            )
        ],
    )


def test_route_structured_completion_returns_parsed_instance_from_provider_json(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(model="fake-model", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    result = route_structured_completion(
        [ChatMessage(role="user", content="hi")],
        model="fake-model",
        response_model=_Extraction,
        call_completion=caller,
        emitter=emitter,
    )

    assert isinstance(result, StructuredCompletionResult)
    assert result.parsed == _Extraction(label="ok", score=3)
    assert result.model == response.model


def test_route_structured_completion_falls_back_to_requested_model_when_response_model_absent(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(model="", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    result = route_structured_completion(
        [ChatMessage(role="user", content="hi")],
        model="fake-model",
        response_model=_Extraction,
        call_completion=caller,
        emitter=emitter,
    )

    assert result.model == "fake-model"


def test_route_structured_completion_forwards_mapped_messages_and_response_format_to_caller(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(model="fake-model", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    route_structured_completion(
        [ChatMessage(role="user", content="hi")],
        model="fake-model",
        response_model=_Extraction,
        timeout=45.0,
        call_completion=caller,
        emitter=emitter,
    )

    assert len(caller.calls) == 1
    call = caller.calls[0]
    assert call["model"] == "fake-model"
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    assert call["timeout"] == 45.0
    assert call["response_format"] is _Extraction


def test_route_structured_completion_marks_llm_interface_healthy_on_success(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(model="fake-model", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    route_structured_completion(
        [ChatMessage(role="user", content="hi")],
        model="fake-model",
        response_model=_Extraction,
        call_completion=caller,
        emitter=emitter,
    )

    assert is_healthy(LLM_INTERFACE) is True


def test_route_structured_completion_binds_response_model_to_parsed_type(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime no-op; the binding is checked statically by `uv run basedpyright`."""
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(model="fake-model", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    result = route_structured_completion(
        [ChatMessage(role="user", content="hi")],
        model="fake-model",
        response_model=_Extraction,
        call_completion=caller,
        emitter=emitter,
    )

    assert_type(result.parsed, _Extraction)
    parsed: _Extraction = result.parsed
    assert parsed is result.parsed


def test_route_structured_completion_is_importable_from_package_front_door() -> None:
    import ps_service.llm_interface as front_door

    for name in (
        "StructuredCompletionCaller",
        "StructuredCompletionResult",
        "default_structured_completion_caller",
        "route_structured_completion",
        "LlmResponseSchemaError",
    ):
        assert hasattr(front_door, name)
        assert name in front_door.__all__


def test_route_structured_completion_emits_one_success_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    log_emitter, log_path = make_emitter()
    response = _fake_response(model="fake-model", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    route_structured_completion(
        [ChatMessage(role="user", content="hi")],
        model="fake-model",
        response_model=_Extraction,
        call_completion=caller,
        emitter=log_emitter,
    )
    log_emitter.flush()

    lines = read_lines(log_path)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["component"] == "llm_interface"
    assert entry["action"] == "route_structured_completion"
    assert entry["outcome"] == "success"
    assert isinstance(entry["duration_ms"], float)
    known_keys = {"component", "action", "outcome", "duration_ms", "timestamp", "run_id"}
    assert set(entry) - known_keys == {"model"}
    assert entry["model"] == "fake-model"


def test_route_structured_completion_raises_before_calling_provider_when_model_lacks_schema_support(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=False)
    response = _fake_response(model="fake-model", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmProviderError) as exc_info:
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    assert "fake-model" in str(exc_info.value)
    assert caller.calls == []


def test_route_structured_completion_unsupported_model_leaves_health_unchanged(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=False)
    response = _fake_response(model="fake-model", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmProviderError):
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    assert is_healthy(LLM_INTERFACE) is True

    mark_unhealthy(LLM_INTERFACE, error=RuntimeError("pre-seeded"))

    with pytest.raises(LlmProviderError):
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    assert is_healthy(LLM_INTERFACE) is False


def test_route_structured_completion_unsupported_model_emits_unsupported_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=False)
    log_emitter, log_path = make_emitter()
    response = _fake_response(model="fake-model", content='{"label": "ok", "score": 3}')
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmProviderError):
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=log_emitter,
        )
    log_emitter.flush()

    lines = read_lines(log_path)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["outcome"] == "unsupported"
    assert isinstance(entry["duration_ms"], float)
    known_keys = {"component", "action", "outcome", "duration_ms", "timestamp", "run_id"}
    assert set(entry) - known_keys == {"model"}
    assert entry["model"] == "fake-model"


def test_route_structured_completion_raises_schema_error_when_content_is_not_json(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(model="fake-model", content="not json at all")
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmResponseSchemaError) as exc_info:
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    assert isinstance(exc_info.value.__cause__, pydantic.ValidationError)


def test_route_structured_completion_raises_schema_error_when_json_does_not_conform(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(
        model="fake-model", content='{"label": "ok", "score": "not-a-number"}'
    )
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmResponseSchemaError) as exc_info:
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    assert isinstance(exc_info.value.__cause__, pydantic.ValidationError)


def test_route_structured_completion_schema_mismatch_leaves_llm_interface_healthy(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(model="fake-model", content="not json at all")
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmResponseSchemaError):
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    assert is_healthy(LLM_INTERFACE) is True


def test_route_structured_completion_schema_error_message_omits_completion_text(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    sentinel = "SECRET-REGULATION-TEXT"
    response = _fake_response(model="fake-model", content=f"not json at all {sentinel}")
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmResponseSchemaError) as exc_info:
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    message = str(exc_info.value)
    assert "fake-model" in message
    assert _Extraction.__name__ in message
    assert sentinel not in message
    assert sentinel in str(exc_info.value.__cause__)


def test_route_structured_completion_schema_mismatch_emits_schema_mismatch_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    sentinel = "SECRET-REGULATION-TEXT"
    log_emitter, log_path = make_emitter()
    response = _fake_response(model="fake-model", content=f"not json at all {sentinel}")
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmResponseSchemaError):
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=log_emitter,
        )
    log_emitter.flush()

    lines = read_lines(log_path)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["outcome"] == "schema_mismatch"
    known_keys = {"component", "action", "outcome", "duration_ms", "timestamp", "run_id"}
    assert set(entry) - known_keys == {"model"}
    assert entry["model"] == "fake-model"

    raw_line = log_path.read_text(encoding="utf-8")
    assert sentinel not in raw_line


def test_route_structured_completion_raises_llm_provider_error_when_provider_call_raises(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)

    def fake_call_completion(
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float,
        response_format: type[pydantic.BaseModel],
    ) -> ModelResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError) as exc_info:
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=fake_call_completion,
            emitter=emitter,
        )

    assert isinstance(exc_info.value.__cause__, openai.APIConnectionError)


def test_route_structured_completion_marks_llm_interface_unhealthy_when_provider_call_raises(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)

    def fake_call_completion(
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float,
        response_format: type[pydantic.BaseModel],
    ) -> ModelResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError):
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=fake_call_completion,
            emitter=emitter,
        )

    assert is_healthy(LLM_INTERFACE) is False


def test_route_structured_completion_provider_error_emits_error_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    log_emitter, log_path = make_emitter()

    def fake_call_completion(
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float,
        response_format: type[pydantic.BaseModel],
    ) -> ModelResponse:
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))

    with pytest.raises(LlmProviderError):
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=fake_call_completion,
            emitter=log_emitter,
        )
    log_emitter.flush()

    lines = read_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["outcome"] == "error"


def test_route_structured_completion_raises_plain_provider_error_when_provider_returns_no_choices(
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = ModelResponse(id="x", model="fake-model", choices=[])
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmProviderError) as exc_info:
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    assert type(exc_info.value) is LlmProviderError
    assert is_healthy(LLM_INTERFACE) is True


def test_route_structured_completion_raises_plain_provider_error_when_provider_returns_empty_content(  # noqa: E501 - name mirrors PLAN.md Slice 3 verbatim
    emitter: LogEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_schema_support(monkeypatch, supported=True)
    response = _fake_response(model="fake-model", content="")
    caller = _RecordingStructuredCaller(response)

    with pytest.raises(LlmProviderError) as exc_info:
        route_structured_completion(
            [ChatMessage(role="user", content="hi")],
            model="fake-model",
            response_model=_Extraction,
            call_completion=caller,
            emitter=emitter,
        )

    assert type(exc_info.value) is LlmProviderError
    assert is_healthy(LLM_INTERFACE) is True
