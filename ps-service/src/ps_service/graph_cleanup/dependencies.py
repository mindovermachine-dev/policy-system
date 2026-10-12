"""Default wiring for `ps_service.graph_cleanup` (issue #190).

`build_default_graph_cleanup_graph_opener` is the zero-business-logic, sole
FalkorDB connection surface for this component (listed in
`docs/coding-standards/approved-mock-boundaries.yaml`); tests substitute it.
The FalkorDB client import is function-local (M6), so importing
`ps_service.main` never loads Company Merge's client at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.api.graph_write_access import graph_write_access
from ps_service.audit.store import PsycopgAuditStore
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.graph_cleanup.graph_submission import cleanup_submitter

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.audit.store import AuditStore
    from ps_service.authz.store import AccessRoleStore
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.config import ServiceConfig
    from ps_service.graph_cleanup.graph_submission import CleanupSubmitter


def _default_audit_store(config: ServiceConfig) -> AuditStore:
    return PsycopgAuditStore(config)


def _default_access_role_store(config: ServiceConfig) -> AccessRoleStore:
    return PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config))


def _default_graph_write_gateway(config: ServiceConfig) -> CleanupSubmitter:
    """The process's Graph Write Gateway, narrowed to what Graph Cleanup needs (issue #212).

    Resolved when a write or a reconciliation runs, never at import: with no gateway installed
    `graph_write_access` raises `GraphWriteUnavailableError`, which the write step maps to the
    persistence error and the reconciler to a deferral.
    """
    return cleanup_submitter(graph_write_access(config).gateway)


@dataclass(frozen=True, slots=True)
class GraphCleanupDependencies:
    """Everything the cleanup tools need that is not per-request.

    `graph_write_gateway` is where every graph write goes (one gateway group per approved action)
    and what the orphan reconciler asks whether the graph has caught up to its log.
    """

    open_single_tenant_graph: Callable[[ServiceConfig], GraphHandle]
    audit_store: Callable[[ServiceConfig], AuditStore] = _default_audit_store
    access_role_store: Callable[[ServiceConfig], AccessRoleStore] = _default_access_role_store
    graph_write_gateway: Callable[[ServiceConfig], CleanupSubmitter] = _default_graph_write_gateway


def _open_single_tenant_graph(config: ServiceConfig) -> GraphHandle:
    """Open the single-tenant (`policy_system`) graph (own copy, per component convention)."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
        select_graph,
        single_tenant_graph_name,
    )

    return select_graph(connect_from_config(config), single_tenant_graph_name())


def build_default_graph_cleanup_graph_opener() -> Callable[[ServiceConfig], GraphHandle]:
    """Return the real single-tenant graph opener (the approved mock boundary)."""
    return _open_single_tenant_graph


def build_default_graph_cleanup_dependencies(
    *,
    open_single_tenant_graph: Callable[[ServiceConfig], GraphHandle] | None = None,
    audit_store: Callable[[ServiceConfig], AuditStore] | None = None,
    access_role_store: Callable[[ServiceConfig], AccessRoleStore] | None = None,
    graph_write_gateway: Callable[[ServiceConfig], CleanupSubmitter] | None = None,
) -> GraphCleanupDependencies:
    """Wire the default `GraphCleanupDependencies`.

    The opener is resolved at call time, so a test that substitutes
    `build_default_graph_cleanup_graph_opener` takes effect with no other change.
    The audit and access-role store factories default to the PostgreSQL stores; the gateway
    defaults to the process's installed Graph Write Gateway.
    """
    defaults = GraphCleanupDependencies(
        open_single_tenant_graph=open_single_tenant_graph
        or build_default_graph_cleanup_graph_opener()
    )
    return GraphCleanupDependencies(
        open_single_tenant_graph=defaults.open_single_tenant_graph,
        audit_store=audit_store or defaults.audit_store,
        access_role_store=access_role_store or defaults.access_role_store,
        graph_write_gateway=graph_write_gateway or defaults.graph_write_gateway,
    )
