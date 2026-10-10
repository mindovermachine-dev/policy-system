"""The Graph Write Gateway and system audit recorder an ingestion run writes its graphs through.

The service builds one `GraphWriteGateway` at startup (`ps_service.main`); the gateway serialises
the writes of each graph with an in-process lock, so every writer in the process must use that
one instance. REST routes can be handed it by dependency injection, but the MCP tools and the
amendment sweep run outside a request, so the lifespan also installs it here, once, for them.

`graph_write_access(config)` is the one way a run obtains it. It fails closed (issue #208): with
no gateway installed (no PS state Postgres, or the gateway could not be built) it raises
`GraphWriteUnavailableError`; there is no direct-write fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.graph_gateway.system_audit import build_system_audit_recorder

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig
    from ps_service.graph_gateway.system_audit import SystemAuditRecorder
    from ps_service.ingestion.graph_submission import GroupSubmitter

GRAPH_WRITE_UNAVAILABLE_MESSAGE = (
    "graph writes are unavailable: the graph write gateway is not running "
    "(the PS state database is not configured)"
)


@dataclass(frozen=True, slots=True)
class GraphWriteAccess:
    """What one ingestion run needs to write a graph: the gateway and a system audit recorder."""

    gateway: GroupSubmitter
    system_audit: SystemAuditRecorder


type SystemAuditFactory = Callable[[ServiceConfig], SystemAuditRecorder]

_gateway: GroupSubmitter | None = None
_system_audit_factory: SystemAuditFactory = build_system_audit_recorder


def install_graph_write_gateway(
    gateway: GroupSubmitter | None, *, system_audit: SystemAuditFactory | None = None
) -> None:
    """Make `gateway` the process's gateway (`None`: there is none), replacing any earlier one.

    Called by the app lifespan after the gateway is built and again with `None` when it is shut
    down. `system_audit` replaces how the audit recorder is built from the config (a test's
    substitute for the PS state Postgres); it defaults to `build_system_audit_recorder`.
    """
    global _gateway, _system_audit_factory  # noqa: PLW0603 -- the process-wide gateway handle, see module docstring
    _gateway = gateway
    _system_audit_factory = system_audit or build_system_audit_recorder


def graph_write_access(config: ServiceConfig) -> GraphWriteAccess:
    """Return the gateway and a system audit recorder bound to `config`.

    Raises:
        GraphWriteUnavailableError: no gateway is installed; nothing may be written.
    """
    if _gateway is None:
        raise GraphWriteUnavailableError(GRAPH_WRITE_UNAVAILABLE_MESSAGE)
    return GraphWriteAccess(gateway=_gateway, system_audit=_system_audit_factory(config))
