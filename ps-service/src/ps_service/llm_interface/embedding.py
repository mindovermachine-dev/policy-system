"""ps_service.llm_interface.embedding — the RouteEmbedding action."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

import openai

from ps_service.dependency_health import LLM_INTERFACE, mark_healthy, mark_unhealthy
from ps_service.llm_interface._logging_support import log
from ps_service.llm_interface.client import EmbeddingCaller, default_embedding_caller
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.llm_interface.models import EmbeddingResult

if TYPE_CHECKING:
    from litellm.types.utils import EmbeddingResponse

    from ps_service.logging.emitter import LogEmitter

_DEFAULT_TIMEOUT_SECONDS = 60.0


def route_embedding(
    text: str,
    *,
    model: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> EmbeddingResult:
    """RouteEmbedding: route an embedding request to the configured LLM Provider via LiteLLM."""
    caller = call_embedding if call_embedding is not None else default_embedding_caller
    started = time.perf_counter()
    try:
        response = caller(model=model, inputs=[text], timeout=timeout)
    except openai.OpenAIError as exc:
        mark_unhealthy(LLM_INTERFACE, error=exc)
        log(
            action="route_embedding", outcome="error", started=started, model=model, emitter=emitter
        )
        raise LlmProviderError(f"RouteEmbedding failed for model {model!r}: {exc}") from exc
    mark_healthy(LLM_INTERFACE)
    result = _to_embedding_result(response, model=model)
    log(action="route_embedding", outcome="success", started=started, model=model, emitter=emitter)
    return result


def _to_embedding_result(response: EmbeddingResponse, *, model: str) -> EmbeddingResult:
    """Build an `EmbeddingResult` from a provider `EmbeddingResponse`.

    Raises `LlmProviderError` for an unexpected/empty shape.
    """
    # litellm's `EmbeddingResponse.data` is an untyped `list`; at this one boundary its
    # runtime shape is a list of dicts each carrying an `"embedding"` float list (dict
    # access — verified empirically, not attribute access).
    data = cast("list[dict[str, list[float]]]", response.data)
    if not data:
        raise LlmProviderError(f"provider returned no embedding data for model {model!r}")
    vector = data[0]["embedding"]
    if not vector:
        raise LlmProviderError(f"provider returned an empty embedding vector for model {model!r}")
    return EmbeddingResult(vector=list(vector), model=response.model or model)
