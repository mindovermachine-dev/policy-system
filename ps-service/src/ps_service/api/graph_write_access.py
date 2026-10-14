"""The Graph Write Gateway and system audit recorder an ingestion run writes its graphs through.

The service builds one `GraphWriteGateway` at startup (`ps_service.main`); the gateway serialises
the writes of each graph with an in-process lock, so every writer in the process must use that
one instance. REST routes can be handed it by dependency injection, but the MCP tools and the
amendment sweep run outside a request, so the lifespan also installs it here, once, for them.

`graph_write_access(config)` is the one way a run obtains it. It fails closed: with
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
    """What one ingestion run needs to write a graph: the gateway and a system audit recorder.

    Attributes:
        gateway: The process's single Graph Write Gateway; it submits each graph's write group
            under that graph's in-process lock.
        system_audit: Recorder for system-actor audit rows, bound to the service configuration
            the access was requested with.
    """

    gateway: GroupSubmitter
    system_audit: SystemAuditRecorder


type SystemAuditFactory = Callable[[ServiceConfig], SystemAuditRecorder]

_gateway: GroupSubmitter | None = None
_system_audit_factory: SystemAuditFactory = build_system_audit_recorder


def install_graph_write_gateway(
    gateway: GroupSubmitter | None, *, system_audit: SystemAuditFactory | None = None
) -> None:
    """Make ``gateway`` the process's gateway (``None``: there is none), replacing any earlier one.

    Called by the app lifespan after the gateway is built and again with ``None`` when it is shut
    down.

    Args:
        gateway: The gateway every ingestion run in this process must write through, or ``None``
            to uninstall it so that later write attempts fail closed.
        system_audit: Factory building the audit recorder from a ``ServiceConfig`` (a test's
            substitute for the PS state Postgres). ``None`` selects
            ``build_system_audit_recorder``.

    Returns:
        None.

    Side Effects:
        Replaces the module-level gateway handle and audit-recorder factory; the previous
        values are discarded. Not thread-safe against concurrent callers; it is called only from
        startup and shutdown.
    """
    global _gateway, _system_audit_factory  # noqa: PLW0603 -- the process-wide gateway handle outlives any request
    _gateway = gateway
    _system_audit_factory = system_audit or build_system_audit_recorder


def graph_write_access(config: ServiceConfig) -> GraphWriteAccess:
    """Return the installed gateway and a system audit recorder bound to ``config``.

    Every graph write of an ingestion run obtains its gateway here, so writes are serialised
    through one instance and fail closed when none is running.

    Args:
        config: Service configuration the audit recorder is built from (it names the PS state
            Postgres the recorder writes to).

    Returns:
        A ``GraphWriteAccess`` holding the installed gateway and a freshly built recorder.

    Raises:
        GraphWriteUnavailableError: no gateway is installed (no PS state Postgres, or the
            gateway could not be built); nothing may be written and there is no direct-write
            fallback. Any error raised by the installed audit-recorder factory also propagates.

    Side Effects:
        None; the recorder is built but opens no connection until it records a row.
    """
    if _gateway is None:
        raise GraphWriteUnavailableError(GRAPH_WRITE_UNAVAILABLE_MESSAGE)
    return GraphWriteAccess(gateway=_gateway, system_audit=_system_audit_factory(config))
