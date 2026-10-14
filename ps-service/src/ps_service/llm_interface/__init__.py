"""Public API for `ps_service.llm_interface`.

Purpose: the single gateway through which PS Service calls its configured LLM Provider
(via LiteLLM) for chat completion, schema-constrained completion and embedding, with
health recording and structured logging applied uniformly. It exports the three routing
functions, the startup readiness probe, the injectable transport seams and their defaults,
the request/result records and the two error types.
"""

from ps_service.llm_interface.client import (
    CompletionCaller,
    EmbeddingCaller,
    StructuredCompletionCaller,
    default_completion_caller,
    default_embedding_caller,
    default_structured_completion_caller,
)
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.connectivity import check_connectivity
from ps_service.llm_interface.embedding import route_embedding
from ps_service.llm_interface.errors import LlmProviderError, LlmResponseSchemaError
from ps_service.llm_interface.models import (
    ChatMessage,
    CompletionResult,
    EmbeddingResult,
    StructuredCompletionResult,
)
from ps_service.llm_interface.structured_completion import route_structured_completion

__all__ = [
    "ChatMessage",
    "CompletionCaller",
    "CompletionResult",
    "EmbeddingCaller",
    "EmbeddingResult",
    "LlmProviderError",
    "LlmResponseSchemaError",
    "StructuredCompletionCaller",
    "StructuredCompletionResult",
    "check_connectivity",
    "default_completion_caller",
    "default_embedding_caller",
    "default_structured_completion_caller",
    "route_completion",
    "route_embedding",
    "route_structured_completion",
]
