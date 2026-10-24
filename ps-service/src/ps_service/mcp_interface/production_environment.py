"""The production `GateEnvironment`: every factory wired to its real store or connection.

This is the one module of the package that names the Postgres store classes and the FalkorDB
connection opener. The tool modules receive the environment and call its factories, so a test
builds a server over its own environment and the deployed server is built over this one.

Business Rules:
    - A store that records audit rows is built over the audit store the environment's own audit
      factory builds, so the audit rows and the store's rows are written through the same wiring.
    - The graph is opened through the read-only connection of the query engine, the same opener
      the company-merge and near-miss paths use, so no tool can open a writable connection.
    - Importing this module performs no I/O; each factory connects when it is called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import load_config
from ps_service.ingestion_runs import PsycopgIngestionRunStore
from ps_service.mcp_interface.tool_core import resolve_authz_actor, run_mcp_action
from ps_service.mcp_interface.tool_gates import GateEnvironment
from ps_service.passkey_signing.store import PsycopgPendingApprovalStore
from ps_service.query_engine.falkordb_client import connect_from_config, select_graph
from ps_service.runtime_config import PsycopgRuntimeConfigStore

if TYPE_CHECKING:
    from ps_service.audit.store import AuditStore
    from ps_service.authz.store import AccessRoleStore
    from ps_service.config import ServiceConfig
    from ps_service.ingestion_runs import IngestionRunStore
    from ps_service.query_engine.falkordb_client import GraphHandle
    from ps_service.runtime_config import RuntimeConfigStore

__all__ = ["build_production_environment"]


def _build_audit_store(config: ServiceConfig) -> PsycopgAuditStore:
    return PsycopgAuditStore(config)


def _build_role_store(config: ServiceConfig, /, *, audit_store: AuditStore) -> AccessRoleStore:
    return PsycopgAccessRoleStore(config, audit_store=audit_store)


def _build_pending_approval_store(config: ServiceConfig) -> PsycopgPendingApprovalStore:
    return PsycopgPendingApprovalStore(config)


def _build_ingestion_run_store(
    config: ServiceConfig, /, *, audit_store: AuditStore
) -> IngestionRunStore:
    return PsycopgIngestionRunStore(config, audit_store=audit_store)


def _build_runtime_config_store(
    config: ServiceConfig, /, *, audit_store: AuditStore
) -> RuntimeConfigStore:
    return PsycopgRuntimeConfigStore(config, audit_store=audit_store)


def _open_graph(config: ServiceConfig) -> GraphHandle:
    return select_graph(connect_from_config(config), config.falkordb_graph)


def build_production_environment() -> GateEnvironment:
    """Return the environment the deployed MCP server runs on.

    Returns:
        A `GateEnvironment` whose factories build the Postgres stores and open the read-only
        FalkorDB connection.
    """
    return GateEnvironment(
        load_config=load_config,
        resolve_actor=resolve_authz_actor,
        audit_store_factory=_build_audit_store,
        role_store_factory=_build_role_store,
        pending_approval_store_factory=_build_pending_approval_store,
        ingestion_run_store_factory=_build_ingestion_run_store,
        runtime_config_store_factory=_build_runtime_config_store,
        graph_factory=_open_graph,
        run_action=run_mcp_action,
    )
