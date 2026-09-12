"""ps_service.llm_interface.structured_completion — the RouteStructuredCompletion action.

Slice 2 adds the provider-capability fail-fast check
(`litellm.utils.supports_response_schema`, bound into this module's namespace so
tests can monkeypatch it deterministically — see PLAN.md §3.1) and the
`pydantic.ValidationError` -> `LlmResponseSchemaError` translation in
`_parse_structured_response`. The capability check is wrapped defensively: any
exception from `supports_response_schema` itself is treated as "unsupported"
rather than propagating a bare exception (CHANGES.md FLAW-1).

Documented divergence from `route_completion`: `route_completion` emits no log
entry on its empty-choices/empty-content branch, but AC-BI-015 requires an
entry on every branch here, so the `LlmProviderError` shape-check branch in
`_parse_structured_response` also logs `outcome="error"`.
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
    """RouteStructuredCompletion: route a chat completion shaped by `response_model`."""
    started = time.perf_counter()
    # FLAW-1: the real `supports_response_schema` only guards its own `get_llm_provider`
    # call; a fallback branch further in is unguarded, so treat any exception from the
    # check itself as "unsupported" rather than let a bare exception escape here.
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

    Raises `LlmProviderError` for an unexpected/empty shape (parity with
    `_to_completion_result`), and `LlmResponseSchemaError` when the content is not
    valid JSON or does not conform.
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
