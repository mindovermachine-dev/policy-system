"""ps_service.llm_interface.connectivity — the startup readiness probe.

Unlike `falkordb_client.check_connectivity`'s cheap `list_graphs()` round-trip,
there is no free "ping" for an LLM Provider — `check_connectivity` here makes
one minimal real `route_completion` call and one minimal real `route_embedding`
call, which is why `main.py` only calls it once at startup, not on every
`/ready` poll (see issue #22).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.dependency_health import LLM_INTERFACE, mark_unhealthy
from ps_service.llm_interface.completion import route_completion
from ps_service.llm_interface.embedding import route_embedding
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.llm_interface.models import ChatMessage

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_PING_MESSAGE = "ping"


def check_connectivity(config: ServiceConfig) -> None:
    """Confirm the configured LLM Provider is reachable for both completion and embedding.

    LLM Interface is a hard-required dependency regardless of `ServiceConfig`'s own optionality
    for `llm_interface_model`/`llm_interface_embed_model` (both `str | None`, unset in
    configurations that never call this function) — raises `LlmProviderError` if either model is
    unconfigured, without ever making a real call, distinct from a configured-but-unreachable
    model.

    `route_completion`/`route_embedding` already record the outcome in
    `ps_service.dependency_health` themselves on every call (this probe included), so a real
    subsequent failure/success during normal operation continues to update the same live signal
    this startup check seeds.
    """
    if config.llm_interface_model is None:
        error = LlmProviderError("PS_LLMINTERFACE_MODEL is not configured")
        mark_unhealthy(LLM_INTERFACE, error=error)
        raise error
    if config.llm_interface_embed_model is None:
        error = LlmProviderError("PS_LLMINTERFACE_EMBED_MODEL is not configured")
        mark_unhealthy(LLM_INTERFACE, error=error)
        raise error

    route_completion(
        [ChatMessage(role="user", content=_PING_MESSAGE)],
        model=config.llm_interface_model,
    )
    route_embedding(_PING_MESSAGE, model=config.llm_interface_embed_model)
