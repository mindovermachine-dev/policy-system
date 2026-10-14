"""Startup readiness probe for the LLM Provider.

Unlike a database connectivity check, an LLM Provider has no free ping, so the probe makes
one minimal real completion call and one minimal real embedding call. That cost is why the
service runs it once at startup rather than on every readiness poll.
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

    The LLM Interface is a required dependency of the running service, although the
    configuration leaves both model settings optional because some configurations never call
    this probe. An unset model is reported as a configuration error before any provider call
    is made, which distinguishes it from a configured model that is unreachable.

    Args:
        config: Service configuration. `llm_interface_model` (chat model) and
            `llm_interface_embed_model` (embedding model) must both be set.

    Returns:
        None. Returning normally means both probe calls succeeded.

    Raises:
        LlmProviderError: A model setting is unset (message names the environment variable;
            no provider call is made), or a probe call failed with a provider error or an
            empty or malformed response.
        LoggingLifecycleError: No log emitter is configured for the process (raised while
            logging a probe call's outcome).

    Side Effects:
        Marks the `llm_interface` dependency unhealthy when a model is unset. Otherwise makes
        one real completion call (message `"ping"`) and one real embedding call (text
        `"ping"`); each call updates the dependency's health and writes a log entry, so later
        calls in normal operation keep the same live signal current.
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
