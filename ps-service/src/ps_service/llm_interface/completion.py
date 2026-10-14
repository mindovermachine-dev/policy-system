"""Chat completion routing for `ps_service.llm_interface`: `route_completion`."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import openai

from ps_service.dependency_health import LLM_INTERFACE, mark_healthy, mark_unhealthy
from ps_service.llm_interface._logging_support import log
from ps_service.llm_interface.client import CompletionCaller, default_completion_caller
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.llm_interface.models import ChatMessage, CompletionResult

if TYPE_CHECKING:
    from litellm.types.utils import ModelResponse

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
    """Send a chat conversation to the configured LLM Provider and return the reply text.

    Args:
        messages: Conversation so far, oldest first. Each `ChatMessage` already enforces a
            valid role and non-empty content; the list itself should be non-empty.
        model: Provider-qualified model identifier, for example `"azure/<deployment>"`.
        timeout: Maximum seconds to wait for the provider; must be positive. Defaults to 60.
        call_completion: Transport to call instead of LiteLLM, for tests or alternative
            providers. None selects `default_completion_caller`.
        emitter: Log emitter for the call's log entry. None uses the process default emitter.

    Returns:
        A `CompletionResult` holding the first choice's text and the model id the provider
        reported serving (falling back to `model` when the provider reports none).

    Raises:
        LlmProviderError: The provider call failed (`openai.OpenAIError` from the transport,
            kept as `__cause__`), or the response had no choices or empty text.
        LoggingLifecycleError: `emitter` is None and no process default emitter has been
            configured (raised while logging the outcome).

    Side Effects:
        Makes one network call to the LLM Provider. Marks the `llm_interface` dependency
        unhealthy on a provider error and healthy otherwise (an empty or malformed response
        still counts as reachable). Emits one log entry (`success` or `error`) carrying the
        model id and duration, never message content; an empty or malformed response raises
        without a log entry.
    """
    caller = call_completion if call_completion is not None else default_completion_caller
    payload = [{"role": m.role, "content": m.content} for m in messages]
    started = time.perf_counter()
    try:
        response = caller(model=model, messages=payload, timeout=timeout)
    except openai.OpenAIError as exc:
        mark_unhealthy(LLM_INTERFACE, error=exc)
        log(
            action="route_completion",
            outcome="error",
            started=started,
            model=model,
            emitter=emitter,
        )
        raise LlmProviderError(f"RouteCompletion failed for model {model!r}: {exc}") from exc
    mark_healthy(LLM_INTERFACE)
    result = _to_completion_result(response, model=model)
    log(action="route_completion", outcome="success", started=started, model=model, emitter=emitter)
    return result


def _to_completion_result(response: ModelResponse, *, model: str) -> CompletionResult:
    """Build a `CompletionResult` from a provider response.

    Args:
        response: The provider's non-streaming chat response.
        model: Requested model id, used when the response does not name the served model.

    Returns:
        A `CompletionResult` with the first choice's text and the served model id.

    Raises:
        LlmProviderError: The response has no choices, or the first choice's text is empty.
    """
    if not response.choices:
        raise LlmProviderError(f"provider returned no choices for model {model!r}")
    content = response.choices[0].message.content
    if not content:
        raise LlmProviderError(f"provider returned empty completion text for model {model!r}")
    return CompletionResult(text=content, model=response.model or model)
