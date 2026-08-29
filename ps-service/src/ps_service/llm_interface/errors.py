"""Domain-specific exception type for ps_service.llm_interface."""

from __future__ import annotations


class LlmProviderError(Exception):
    """A RouteCompletion/RouteEmbedding call to the configured LLM Provider (via LiteLLM) failed.

    Covers a rate limit, timeout, auth failure, or an unexpected/empty response shape. Always
    raised via `raise LlmProviderError(...) from exc` so the original litellm/openai exception is
    preserved as `__cause__`.
    """
