"""Exception types raised by `ps_service.llm_interface`."""

from __future__ import annotations


class LlmProviderError(Exception):
    """A call to the configured LLM Provider (through LiteLLM) failed.

    Purpose: give callers one domain error for every way an LLM call can fail, so they need
    not depend on provider or LiteLLM exception types.

    Raised for a rate limit, timeout, authentication failure or other provider error, for an
    unexpected or empty response shape, for a model that is not configured, and for a model
    that does not support provider-enforced response schemas. When a provider exception
    caused it, that exception is preserved as `__cause__`.
    """


class LlmResponseSchemaError(LlmProviderError):
    """The provider's response did not conform to the requested Pydantic response model.

    Purpose: signal a data-shape failure of one call, which is never evidence that the
    provider is unreachable; the dependency stays recorded as healthy. It is a subclass of
    `LlmProviderError`, so handlers of the base error also catch it.

    The message names only the model id and the response model's class name. The offending
    completion text is reachable only through `__cause__` (the `pydantic.ValidationError`),
    so a caller can log `str(exc)` without leaking regulation-derived content.

    Do not log this exception through `logger.exception(...)` or let it reach an unhandled
    traceback: Python's exception chaining prints `__cause__`'s full message, including the
    raw completion text in the validation error's `input_value` fields. In any outward-facing
    or audit path log only `str(exc)`, never the exception object or its traceback.
    """
