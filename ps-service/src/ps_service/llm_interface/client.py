"""Injectable transport seams for `ps_service.llm_interface`, and their LiteLLM defaults.

Purpose: keep the routing functions independent of `litellm` so tests can substitute a
fake transport and the routing code never builds provider clients inline. The caller
types are structural protocols; the `default_*` functions are the real LiteLLM-backed
implementations used when no caller is injected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

import litellm

if TYPE_CHECKING:
    from litellm.types.utils import EmbeddingResponse, ModelResponse
    from pydantic import BaseModel


class CompletionCaller(Protocol):
    """The transport `route_completion` calls to obtain a chat completion from a provider."""

    def __call__(
        self, *, model: str, messages: list[dict[str, str]], timeout: float
    ) -> ModelResponse:
        """Request a chat completion from the provider.

        Args:
            model: Provider-qualified model identifier, for example `"azure/<deployment>"`.
            messages: Conversation so far as `{"role": ..., "content": ...}` dicts, oldest
                first; must be non-empty.
            timeout: Maximum seconds to wait for the provider; must be positive.

        Returns:
            The provider's `ModelResponse`, with at least one choice on success.

        Raises:
            openai.OpenAIError: The provider call failed (rate limit, timeout,
                authentication, connection or server error). `route_completion` catches it and
                re-raises `LlmProviderError`.

        Side Effects:
            Makes one network call to the LLM Provider.
        """
        ...


class StructuredCompletionCaller(Protocol):
    """The transport `route_structured_completion` calls for a schema-constrained completion."""

    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        timeout: float,
        response_format: type[BaseModel],
    ) -> ModelResponse:
        """Request a completion whose content the provider constrains to a response schema.

        Args:
            model: Provider-qualified model identifier; must support provider-enforced
                response schemas.
            messages: Conversation so far as `{"role": ..., "content": ...}` dicts, oldest
                first; must be non-empty.
            timeout: Maximum seconds to wait for the provider; must be positive.
            response_format: Pydantic model class whose JSON schema the provider enforces on
                the completion content.

        Returns:
            The provider's `ModelResponse`, whose first choice holds JSON text intended to
            conform to `response_format`.

        Raises:
            openai.OpenAIError: The provider call failed (rate limit, timeout,
                authentication, connection or server error). `route_structured_completion`
                catches it and re-raises `LlmProviderError`.

        Side Effects:
            Makes one network call to the LLM Provider.
        """
        ...


class EmbeddingCaller(Protocol):
    """The transport `route_embedding` calls to obtain embedding vectors from a provider."""

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        """Request embeddings from the provider.

        Args:
            model: Provider-qualified embedding model identifier.
            inputs: Texts to embed; must be non-empty, each text non-empty.
            timeout: Maximum seconds to wait for the provider; must be positive.

        Returns:
            The provider's `EmbeddingResponse`, with one vector per input on success.

        Raises:
            openai.OpenAIError: The provider call failed (rate limit, timeout,
                authentication, connection or server error). `route_embedding` catches it
                and re-raises `LlmProviderError`.

        Side Effects:
            Makes one network call to the LLM Provider.
        """
        ...


def default_completion_caller(
    *, model: str, messages: list[dict[str, str]], timeout: float
) -> ModelResponse:
    """Request a chat completion through LiteLLM; the default `CompletionCaller`.

    Credentials are resolved by LiteLLM from its own provider environment variables
    (`AZURE_API_KEY`, `AZURE_API_BASE` and `AZURE_API_VERSION` for `azure/*` models) and are
    never passed explicitly.

    Args:
        model: Provider-qualified model identifier, for example `"azure/<deployment>"`.
        messages: Conversation so far as `{"role": ..., "content": ...}` dicts, oldest
            first; must be non-empty.
        timeout: Maximum seconds to wait for the provider; must be positive.

    Returns:
        The provider's non-streaming `ModelResponse`.

    Raises:
        openai.OpenAIError: The provider call failed (rate limit, timeout, authentication,
            connection or server error). LiteLLM maps provider errors onto this hierarchy.

    Side Effects:
        Makes one network call to the LLM Provider.
    """
    response = litellm.completion(  # pyright: ignore[reportUnknownMemberType]  # litellm: untyped re-export, no py.typed
        model=model, messages=messages, timeout=timeout
    )
    # completion()'s static return type is ModelResponse | CustomStreamWrapper; the stream
    # branch only occurs when stream=True is passed, which this wrapper never does.
    return cast("ModelResponse", response)


def default_structured_completion_caller(
    *,
    model: str,
    messages: list[dict[str, str]],
    timeout: float,
    response_format: type[BaseModel],
) -> ModelResponse:
    """Request a schema-constrained completion through LiteLLM; the default caller.

    Credentials are resolved by LiteLLM from its own provider environment variables and are
    never passed explicitly.

    Args:
        model: Provider-qualified model identifier; must support provider-enforced response
            schemas.
        messages: Conversation so far as `{"role": ..., "content": ...}` dicts, oldest
            first; must be non-empty.
        timeout: Maximum seconds to wait for the provider; must be positive.
        response_format: Pydantic model class whose JSON schema the provider enforces on the
            completion content.

    Returns:
        The provider's non-streaming `ModelResponse`.

    Raises:
        openai.OpenAIError: The provider call failed (rate limit, timeout, authentication,
            connection or server error). LiteLLM maps provider errors onto this hierarchy.

    Side Effects:
        Makes one network call to the LLM Provider.
    """
    response = litellm.completion(  # pyright: ignore[reportUnknownMemberType]  # litellm: untyped re-export, no py.typed
        model=model, messages=messages, timeout=timeout, response_format=response_format
    )
    # completion()'s static return type is ModelResponse | CustomStreamWrapper; the stream
    # branch only occurs when stream=True is passed, which this wrapper never does.
    return cast("ModelResponse", response)


def default_embedding_caller(*, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
    """Request embeddings through LiteLLM; the default `EmbeddingCaller`.

    Credentials are resolved by LiteLLM from its own provider environment variables (for
    `azure/*` models `AZURE_API_KEY`, `AZURE_API_BASE` and `AZURE_API_VERSION`) and are never
    passed explicitly.

    Args:
        model: Provider-qualified embedding model identifier.
        inputs: Texts to embed; must be non-empty, each text non-empty.
        timeout: Maximum seconds to wait for the provider. Truncated to whole seconds before
            the call, because LiteLLM's embedding call accepts an integer timeout.

    Returns:
        The provider's `EmbeddingResponse`, with one vector per input.

    Raises:
        openai.OpenAIError: The provider call failed (rate limit, timeout, authentication,
            connection or server error). LiteLLM maps provider errors onto this hierarchy.

    Side Effects:
        Makes one network call to the LLM Provider.
    """
    # litellm.embedding's sync overload types `timeout` as `int` (seconds); the seam
    # carries `float` for parity with the completion path, narrowed here at the one boundary.
    return litellm.embedding(  # pyright: ignore[reportUnknownMemberType]  # litellm: untyped re-export, no py.typed
        model=model, input=inputs, timeout=int(timeout)
    )
