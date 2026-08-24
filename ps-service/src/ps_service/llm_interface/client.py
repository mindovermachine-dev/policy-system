"""The DI seam for ps_service.llm_interface (L2: "must not construct its own
infrastructure clients inline"). `CompletionCaller`/`EmbeddingCaller` are
`Protocol`s (structural), mirroring `ps_service.logging.emitter`'s
`TextSink`/`WriterFactory` pattern — `route_completion`/`route_embedding`
depend on these abstractions, never on `litellm` directly.
"""

from __future__ import annotations

from typing import Protocol, cast

import litellm
from litellm.types.utils import EmbeddingResponse, ModelResponse


class CompletionCaller(Protocol):
    """The transport seam `route_completion` calls through."""

    def __call__(self, *, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse: ...


class EmbeddingCaller(Protocol):
    """The transport seam `route_embedding` calls through."""

    def __call__(self, *, model: str, input: list[str], timeout: float) -> EmbeddingResponse: ...


def default_completion_caller(*, model: str, messages: list[dict[str, str]], timeout: float) -> ModelResponse:
    """Calls the real litellm.completion. Credentials resolved by LiteLLM from its own
    provider env vars (AZURE_API_KEY/AZURE_API_BASE/AZURE_API_VERSION for azure/* models) —
    never passed explicitly here, per L2 Configuration & Secrets."""
    response = litellm.completion(model=model, messages=messages, timeout=timeout)
    # completion()'s static return type is ModelResponse | CustomStreamWrapper; the stream
    # branch only occurs when stream=True is passed, which this wrapper never does.
    return cast(ModelResponse, response)


def default_embedding_caller(*, model: str, input: list[str], timeout: float) -> EmbeddingResponse:
    """Calls the real litellm.embedding. Credentials resolved by LiteLLM the same way as
    default_completion_caller — never passed explicitly here."""
    response = litellm.embedding(model=model, input=input, timeout=timeout)
    # Same reasoning: the Coroutine branch only occurs when aembedding=True is passed.
    return cast(EmbeddingResponse, response)
