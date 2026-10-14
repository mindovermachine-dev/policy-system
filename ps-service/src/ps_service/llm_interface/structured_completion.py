"""Schema-constrained completion routing for `ps_service.llm_interface`.

Purpose: `route_structured_completion` asks the provider for a reply that conforms to a
Pydantic model and returns it parsed. It fails fast when the model cannot enforce response
schemas, and separates a non-conforming reply (a data-shape failure) from a provider failure.

The provider-capability check uses `litellm.utils.supports_response_schema`, imported into
this module's namespace so tests can replace it. Any exception from that check is treated as
"unsupported" rather than escaping.

Unlike `route_completion`, every failure branch here is logged, including an empty or
malformed reply, which is logged with outcome `error`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import openai
import pydantic
from litellm.utils import supports_response_schema
from pydantic import BaseModel

from ps_service.dependency_health import LLM_INTERFACE, mark_healthy, mark_unhealthy
from ps_service.llm_interface._logging_support import log
from ps_service.llm_interface.client import (
    StructuredCompletionCaller,
    default_structured_completion_caller,
)
from ps_service.llm_interface.errors import LlmProviderError, LlmResponseSchemaError
from ps_service.llm_interface.models import ChatMessage, StructuredCompletionResult

if TYPE_CHECKING:
    from litellm.types.utils import ModelResponse

    from ps_service.logging.emitter import LogEmitter

_DEFAULT_TIMEOUT_SECONDS = 60.0
_ACTION = "route_structured_completion"


def route_structured_completion[T: BaseModel](
    messages: list[ChatMessage],
    *,
    model: str,
    response_model: type[T],
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    call_completion: StructuredCompletionCaller | None = None,
    emitter: LogEmitter | None = None,
) -> StructuredCompletionResult[T]:
    """Send a chat conversation and return the reply parsed into `response_model`.

    Args:
        messages: Conversation so far, oldest first. Each `ChatMessage` already enforces a
            valid role and non-empty content; the list itself should be non-empty.
        model: Provider-qualified model identifier. It must support provider-enforced
            response schemas.
        response_model: Pydantic model class the reply must conform to; its JSON schema is
            sent to the provider.
        timeout: Maximum seconds to wait for the provider; must be positive. Defaults to 60.
        call_completion: Transport to call instead of LiteLLM, for tests or alternative
            providers. None selects `default_structured_completion_caller`.
        emitter: Log emitter for the call's log entry. None uses the process default emitter.

    Returns:
        A `StructuredCompletionResult` holding the validated `response_model` instance and
        the model id the provider reported serving (falling back to `model` when none).

    Raises:
        LlmProviderError: The model does not support response schemas (or checking support
            raised), the provider call failed (`openai.OpenAIError` from the transport, kept
            as `__cause__`), or the response had no choices or empty text.
        LlmResponseSchemaError: The reply text was not valid JSON or did not conform to
            `response_model`. The message omits the reply text; it is reachable only through
            `__cause__`.
        LoggingLifecycleError: `emitter` is None and no process default emitter has been
            configured (raised while logging the outcome).

    Side Effects:
        Makes one network call to the LLM Provider, except when the model is unsupported.
        Marks the `llm_interface` dependency unhealthy on a provider error and healthy once a
        response arrives (a non-conforming reply still counts as reachable). Emits one log
        entry with outcome `success`, `error`, `unsupported` or `schema_mismatch`, carrying
        the model id and duration and never message or reply content.
    """
    started = time.perf_counter()
    # The real `supports_response_schema` only guards its own provider lookup; a fallback
    # branch further in is unguarded, so any exception from the check itself is treated as
    # "unsupported" rather than letting a bare exception escape.
    try:
        unsupported = not supports_response_schema(model)
    except Exception:  # noqa: BLE001
        unsupported = True
    if unsupported:
        log(action=_ACTION, outcome="unsupported", started=started, model=model, emitter=emitter)
        raise LlmProviderError(
            f"model {model!r} does not support provider-enforced response schemas"
        )
    caller = (
        call_completion if call_completion is not None else default_structured_completion_caller
    )
    payload = [{"role": m.role, "content": m.content} for m in messages]
    try:
        response = caller(
            model=model, messages=payload, timeout=timeout, response_format=response_model
        )
    except openai.OpenAIError as exc:
        mark_unhealthy(LLM_INTERFACE, error=exc)
        log(action=_ACTION, outcome="error", started=started, model=model, emitter=emitter)
        raise LlmProviderError(
            f"RouteStructuredCompletion failed for model {model!r}: {exc}"
        ) from exc
    mark_healthy(LLM_INTERFACE)
    try:
        parsed = _parse_structured_response(response, response_model=response_model, model=model)
    except LlmResponseSchemaError:
        log(
            action=_ACTION, outcome="schema_mismatch", started=started, model=model, emitter=emitter
        )
        raise
    except LlmProviderError:
        log(action=_ACTION, outcome="error", started=started, model=model, emitter=emitter)
        raise
    result = StructuredCompletionResult(parsed=parsed, model=response.model or model)
    log(action=_ACTION, outcome="success", started=started, model=model, emitter=emitter)
    return result


def _parse_structured_response[T: BaseModel](
    response: ModelResponse, *, response_model: type[T], model: str
) -> T:
    """Validate the provider's first-choice content against `response_model`.

    Args:
        response: The provider's non-streaming chat response.
        response_model: Pydantic model class the content must conform to.
        model: Requested model id, used in error messages.

    Returns:
        The validated `response_model` instance.

    Raises:
        LlmProviderError: The response has no choices, or the first choice's text is empty.
        LlmResponseSchemaError: The content is not valid JSON or does not conform to
            `response_model`; the validation error is kept as `__cause__`.
    """
    if not response.choices:
        raise LlmProviderError(f"provider returned no choices for model {model!r}")
    content = response.choices[0].message.content
    if not content:
        raise LlmProviderError(f"provider returned empty completion text for model {model!r}")
    try:
        return response_model.model_validate_json(content)
    except pydantic.ValidationError as exc:
        raise LlmResponseSchemaError(
            f"provider response for model {model!r} did not conform to {response_model.__name__}"
        ) from exc
