"""Embedding routing for `ps_service.llm_interface`: `route_embedding`."""

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
    """Embed one text with the configured LLM Provider and return its vector.

    Args:
        text: The text to embed; must be non-empty.
        model: Provider-qualified embedding model identifier.
        timeout: Maximum seconds to wait for the provider; must be positive. Defaults to 60.
        call_embedding: Transport to call instead of LiteLLM, for tests or alternative
            providers. None selects `default_embedding_caller`.
        emitter: Log emitter for the call's log entry. None uses the process default emitter.

    Returns:
        An `EmbeddingResult` holding the vector and the model id the provider reported
        serving (falling back to `model` when the provider reports none).

    Raises:
        LlmProviderError: The provider call failed (`openai.OpenAIError` from the transport,
            kept as `__cause__`), or the response had no embedding data or an empty vector.
        LoggingLifecycleError: `emitter` is None and no process default emitter has been
            configured (raised while logging the outcome).

    Side Effects:
        Makes one network call to the LLM Provider. Marks the `llm_interface` dependency
        unhealthy on a provider error and healthy otherwise (an empty or malformed response
        still counts as reachable). Emits one log entry (`success` or `error`) carrying the
        model id and duration, never the text; an empty or malformed response raises without
        a log entry.
    """
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
    """Build an `EmbeddingResult` from a provider response.

    Args:
        response: The provider's embedding response.
        model: Requested model id, used when the response does not name the served model.

    Returns:
        An `EmbeddingResult` with the first vector and the served model id.

    Raises:
        LlmProviderError: The response has no embedding data, or the first vector is empty.
    """
    # litellm's `EmbeddingResponse.data` is an untyped `list`; at runtime it is a list of
    # dicts, each carrying an `"embedding"` float list, read by key rather than attribute.
    data = cast("list[dict[str, list[float]]]", response.data)
    if not data:
        raise LlmProviderError(f"provider returned no embedding data for model {model!r}")
    vector = data[0]["embedding"]
    if not vector:
        raise LlmProviderError(f"provider returned an empty embedding vector for model {model!r}")
    return EmbeddingResult(vector=list(vector), model=response.model or model)
