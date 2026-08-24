"""Public API for ps_service.llm_interface: RouteCompletion and RouteEmbedding."""

from ps_service.llm_interface.client import (
    CompletionCaller,
    EmbeddingCaller,
    default_completion_caller,
    default_embedding_caller,
)
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.connectivity import check_connectivity
from ps_service.llm_interface.embedding import route_embedding
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.llm_interface.models import (
    ChatMessage,
    CompletionResult,
    EmbeddingResult,
)

__all__ = [
    "ChatMessage",
    "CompletionCaller",
    "CompletionResult",
    "EmbeddingCaller",
    "EmbeddingResult",
    "LlmProviderError",
    "check_connectivity",
    "default_completion_caller",
    "default_embedding_caller",
    "route_completion",
    "route_embedding",
]
