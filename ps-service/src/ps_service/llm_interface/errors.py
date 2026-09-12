"""Domain-specific exception type for ps_service.llm_interface."""

from __future__ import annotations


class LlmProviderError(Exception):
    """A RouteCompletion/RouteEmbedding call to the configured LLM Provider (via LiteLLM) failed.

    Covers a rate limit, timeout, auth failure, or an unexpected/empty response shape. Always
    raised via `raise LlmProviderError(...) from exc` so the original litellm/openai exception is
    preserved as `__cause__`.
    """


class LlmResponseSchemaError(LlmProviderError):
    """The provider's response did not conform to the requested Pydantic response model.

    A data-shape failure of one call, never evidence the provider is unreachable — the
    caller is left healthy. The message names only the model id and the response model's
    class name; the offending completion text is reachable only via `__cause__` (the
    `pydantic.ValidationError`), so a caller can log `str(exc)` without leaking
    regulation-derived content (L1 "Never log secrets, tokens, or PII").

    Do not log this exception via `logger.exception(...)` or let it surface in an
    unhandled traceback — Python's default exception-chaining renders (`The above
    exception was the direct cause of...`) print `__cause__`'s full message, including
    the raw completion text embedded in `pydantic.ValidationError`'s `input_value=...`
    fields. Log only `str(exc)`, never the exception object or its traceback, in any
    outward-facing/audit path.
    """
