"""ps_service.llm_interface.completion — the RouteCompletion action."""

from __future__ import annotations

import time

import openai
from litellm.types.utils import ModelResponse

from ps_service.llm_interface._logging_support import _log
from ps_service.llm_interface.client import CompletionCaller, default_completion_caller
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.llm_interface.models import ChatMessage, CompletionResult
from ps_service.logging.emitter import LogEmitter

_DEFAULT_TIMEOUT_SECONDS = 60.0


def route_completion(
    messages: list[ChatMessage],
    *,
    model: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    call_completion: CompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> CompletionResult:
    """RouteCompletion: route a chat completion request to the configured LLM Provider via LiteLLM."""
    caller = call_completion if call_completion is not None else default_completion_caller
    payload = [{"role": m.role, "content": m.content} for m in messages]
    started = time.perf_counter()
    try:
        response = caller(model=model, messages=payload, timeout=timeout)
        result = _to_completion_result(response, model=model)
    except openai.OpenAIError as exc:
        _log(action="route_completion", outcome="error", started=started, model=model, emitter=emitter)
        raise LlmProviderError(f"RouteCompletion failed for model {model!r}: {exc}") from exc
    _log(action="route_completion", outcome="success", started=started, model=model, emitter=emitter)
    return result


def _to_completion_result(response: ModelResponse, *, model: str) -> CompletionResult:
    """Build a `CompletionResult` from a provider `ModelResponse`, or raise `LlmProviderError`
    for an unexpected/empty shape."""
    if not response.choices:
        raise LlmProviderError(f"provider returned no choices for model {model!r}")
    content = response.choices[0].message.content
    if not content:
        raise LlmProviderError(f"provider returned empty completion text for model {model!r}")
    return CompletionResult(text=content, model=response.model or model)
