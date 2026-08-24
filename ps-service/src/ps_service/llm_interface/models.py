"""ps_service.llm_interface record shapes — request/response types for
RouteCompletion/RouteEmbedding. Pydantic, frozen (L1 Immutability by Default).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """One role-tagged message in a `route_completion` conversation."""

    model_config = ConfigDict(frozen=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class CompletionResult(BaseModel):
    """The text and served model id returned by `route_completion`."""

    model_config = ConfigDict(frozen=True)
    text: str
    model: str  # the model id the provider actually served (response.model)


class EmbeddingResult(BaseModel):
    """The vector and served model id returned by `route_embedding`."""

    model_config = ConfigDict(frozen=True)
    vector: list[float]
    model: str
