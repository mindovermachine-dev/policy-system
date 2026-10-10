"""Gateway-side doubles for the ``POST /ingestions`` (internal) route tests (issue #208).

The route needs a Graph Write Gateway and a system-audit recorder (it fails closed without a
gateway). Postgres and FalkorDB are the true boundaries: the gateway is the gateway rig over
in-memory fakes, and the audit recorder hands out ids the rig's log accepts.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from graph_gateway._fakes import GatewayRig

from ps_service.api.dependencies import provide_graph_write_gateway, provide_system_audit_recorder
from ps_service.api.graph_write_access import install_graph_write_gateway

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from fastapi import FastAPI


class RecordingSystemAudit:
    """A ``SystemAuditRecorder`` over a rig's log; returns ids the log accepts."""

    def __init__(self, rig: GatewayRig, *, error: Exception | None = None) -> None:
        """Bind to ``rig``; with ``error`` set, every call raises it (an audit outage)."""
        self.rows: list[dict[str, object]] = []
        self.ids: list[str] = []
        self._rig = rig
        self._error = error

    def __call__(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        details: Mapping[str, object],
    ) -> str:
        """Record the row and return its (fresh) id."""
        if self._error is not None:
            raise self._error
        audit_event_id = str(uuid.uuid4())
        self._rig.store.audit_rows.add(audit_event_id)
        self.ids.append(audit_event_id)
        self.rows.append(
            {
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": dict(details),
            }
        )
        return audit_event_id


def install_process_gateway(rig: GatewayRig | None, system_audit: RecordingSystemAudit) -> None:
    """Install the rig's gateway (``None``: none) as the process's, with ``system_audit``.

    What the lifespan does in production, for the callers that run outside a request: the
    catalog pipeline, the MCP tools and the amendment sweep.
    """
    install_graph_write_gateway(
        rig.gateway if rig is not None else None, system_audit=lambda _config: system_audit
    )


def override_gateway(
    app: FastAPI, rig: GatewayRig | None, system_audit: RecordingSystemAudit
) -> None:
    """Make ``app`` resolve the rig's gateway (``None``: no gateway) and ``system_audit``."""
    gateway = rig.gateway if rig is not None else None
    app.dependency_overrides[provide_graph_write_gateway] = lambda: gateway
    app.dependency_overrides[provide_system_audit_recorder] = lambda: system_audit


@dataclass(frozen=True, slots=True)
class ProcessGateway:
    """The gateway rig and audit recorder installed as the process's (what a pipeline run uses)."""

    rig: GatewayRig
    system_audit: RecordingSystemAudit


_CURRENT: list[ProcessGateway] = []


def installed_process_gateway() -> ProcessGateway:
    """The process gateway ``process_gateway_installed`` has installed (for a stage fake that
    projects what the gateway wrote into a generic graph fake).
    """
    return _CURRENT[-1]


@contextmanager
def process_gateway_installed() -> Generator[ProcessGateway]:
    """Install a fresh rig gateway and recording audit as the process's; uninstall on exit.

    The state the app lifespan creates in production, for the catalog pipeline, the MCP tools
    and the amendment sweep, which run outside a request and cannot be injected.
    """
    rig = GatewayRig()
    system_audit = RecordingSystemAudit(rig)
    install_process_gateway(rig, system_audit)
    installed = ProcessGateway(rig, system_audit)
    _CURRENT.append(installed)
    try:
        yield installed
    finally:
        _CURRENT.pop()
        install_graph_write_gateway(None)
        rig.close()
