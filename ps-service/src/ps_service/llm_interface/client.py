"""The DI seam for ps_service.llm_interface.

L2: components "must not construct their own infrastructure clients inline".
`CompletionCaller`/`EmbeddingCaller` are structural `Protocol`s, mirroring
`ps_service.logging.emitter`'s `TextSink`/`WriterFactory` pattern —
`route_completion`/`route_embedding` depend on these abstractions, never on
`litellm` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import litellm

if TYPE_CHECKING:
    from litellm.types.utils import EmbeddingResponse, ModelResponse


class CompletionCaller(Protocol):
    """The transport seam `route_completion` calls through."""

    def __call__(
        self, *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        """Invoke the completion transport."""
        ...


class EmbeddingCaller(Protocol):
    """The transport seam `route_embedding` calls through."""

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        """Invoke the embedding transport."""
        ...


def default_completion_caller(
    *, model: str, messages: list[dict[str, str]], timeout: float
) -> ModelResponse:
    """Call the real `litellm.completion`.

    Credentials are resolved by LiteLLM from its own provider env vars
    (AZURE_API_KEY/AZURE_API_BASE/AZURE_API_VERSION for azure/* models) — never passed explicitly
    here, per L2 Configuration & Secrets.
    """
    response = litellm.completion(  # pyright: ignore[reportUnknownMemberType]  # litellm: untyped re-export, no py.typed
        model=model, messages=messages, timeout=timeout
    )
    # completion()'s static return type is ModelResponse | CustomStreamWrapper; the stream
    # branch only occurs when stream=True is passed, which this wrapper never does.
    return cast("ModelResponse", response)


def default_embedding_caller(*, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
    """Call the real `litellm.embedding`.

    Credentials are resolved by LiteLLM the same way as `default_completion_caller` — never passed
    explicitly here.
    """
    # litellm.embedding's sync overload types `timeout` as `int` (seconds); the seam
    # carries `float` for parity with the completion path, narrowed here at the one boundary.
    return litellm.embedding(  # pyright: ignore[reportUnknownMemberType]  # litellm: untyped re-export, no py.typed
        model=model, input=inputs, timeout=int(timeout)
    )
