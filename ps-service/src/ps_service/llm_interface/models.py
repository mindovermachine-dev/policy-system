"""Request and result records for `ps_service.llm_interface`.

All records are immutable Pydantic models.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """One role-tagged message in a chat conversation sent to the LLM Provider.

    Attributes:
        role: Who authored the message: `"system"`, `"user"` or `"assistant"`.
        content: The message text; must be non-empty (validation fails on an empty string).
    """

    model_config = ConfigDict(frozen=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class CompletionResult(BaseModel):
    """The result of a chat completion.

    Attributes:
        text: The first choice's reply text; non-empty when produced by `route_completion`.
        model: The model id the provider reported serving, or the requested model id when the
            provider reported none.
    """

    model_config = ConfigDict(frozen=True)
    text: str
    model: str


class StructuredCompletionResult[T: BaseModel](BaseModel):
    """The result of a schema-constrained completion.

    Attributes:
        parsed: The reply validated into an instance of the requested response model.
        model: The model id the provider reported serving, or the requested model id when the
            provider reported none.
    """

    model_config = ConfigDict(frozen=True)
    parsed: T
    model: str


class EmbeddingResult(BaseModel):
    """The result of an embedding request.

    Attributes:
        vector: The embedding as a list of floats; non-empty when produced by
            `route_embedding`.
        model: The model id the provider reported serving, or the requested model id when the
            provider reported none.
    """

    model_config = ConfigDict(frozen=True)
    vector: list[float]
    model: str
