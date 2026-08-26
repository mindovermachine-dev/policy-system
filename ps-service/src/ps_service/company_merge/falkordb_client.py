"""FalkorDB connection surface for `ps_service.company_merge`.

Own copy of `ps_service.domain_mapper.falkordb_client`'s shape
(PLAN_REVIEWED.md §0.3 -- a deliberate near-duplicate, not a shared import):
`GraphHandle`/`GraphQueryResult` Protocols, `connect`/`connect_from_config`/
`select_graph`, and this component's own graph-naming helper.

Naming (PLAN_REVIEWED.md §1): `single_tenant_graph_name()` mirrors
`ps_service.mcp_interface.mcp_server`'s existing
`os.environ.get("PS_FALKORDB_GRAPH", "policy_system")` convention exactly --
confirmed directly against that module's own `DEFAULT_GRAPH` line, not
assumed by analogy. Company Merge reads/writes the SAME single-tenant graph
`mcp_interface` exposes read-only Cypher access to; this is not a new graph
name, just this component's own accessor for the existing convention.
"""

from __future__ import annotations

import os
from typing import Protocol

from falkordb import (  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker; this is the one boundary import
    FalkorDB,
)

from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.config import ServiceConfig
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy

_DEFAULT_SINGLE_TENANT_GRAPH_NAME = "policy_system"


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult` -- the one field any
    caller of `GraphHandle.query` needs to read off a response."""

    @property
    def result_set(self) -> list[object]: ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph` -- the `query()` call
    surface this component's graph reader/dedup/writer/merge logic needs:
    bare Cypher, or Cypher + `params=` for parameterized reads/writes.
    Callers outside this module never import `falkordb.Graph` directly."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult: ...


class _ConnectivityProbe(Protocol):
    """Structural requirement for `check_connectivity`'s `db` parameter --
    only the one call surface needed to verify connectivity. A real
    `connect()`-returned `FalkorDB` instance satisfies this structurally
    with no change; tests can substitute a lightweight fake instead of
    mocking the real client type."""

    def list_graphs(self) -> list[str]: ...


def connect(host: str, port: int) -> FalkorDB:
    """Construct a FalkorDB client. Does not itself verify connectivity --
    the underlying client connects lazily on first use; call
    `check_connectivity()` against the result to fail loud early."""
    return FalkorDB(host=host, port=port)


def connect_from_config(config: ServiceConfig) -> FalkorDB:
    """Build the real FalkorDB connection from `config.falkordb_host`/
    `config.falkordb_port` -- mirrors `ps_service.domain_mapper.
    falkordb_client.connect_from_config` exactly. Callers build `config` via
    `ps_service.config.load_config()`, never by constructing a
    `ServiceConfig` with hardcoded host/port values.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def check_connectivity(db: _ConnectivityProbe, host: str, port: int) -> None:
    """Fail loud, with a friendly message, before doing any real work.

    Raises `CompanyMergeConfigurationError` (wrapping the underlying cause)
    if `db` cannot list its graphs -- the cheapest real round-trip available
    to confirm the connection is actually usable. Records the outcome in
    `ps_service.dependency_health` either way, so a caller using this as a
    startup probe also feeds the live readiness signal.
    """
    try:
        db.list_graphs()
    except Exception as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise CompanyMergeConfigurationError(
            f"FalkorDB connection failed at {host}:{port}. "
            f"Is FalkorDB running? Error: {exc}"
        ) from exc
    mark_healthy(FALKORDB)


def select_graph(db: FalkorDB, name: str) -> GraphHandle:
    """The single conversion site from a real `falkordb.Graph` to this
    module's local `GraphHandle` Protocol -- callers elsewhere in
    `ps_service.company_merge` never import `falkordb.Graph` directly.

    `name` is expected to already be the final graph name (e.g. the result
    of `single_tenant_graph_name()`, or a `{short}_baseline` name built
    elsewhere) -- this function does no naming/namespacing of its own.
    """
    return db.select_graph(name)


def single_tenant_graph_name() -> str:
    """The company's merged single-tenant regulatory graph name --
    `PS_FALKORDB_GRAPH`, defaulting to `"policy_system"`. Mirrors
    `ps_service.mcp_interface.mcp_server`'s `DEFAULT_GRAPH` convention
    exactly (verified directly against that module's own source, not
    assumed by analogy) -- this is the one graph name shared across the
    whole system for read-only query access and Company Merge's own
    add/merge-only writes.
    """
    return os.environ.get("PS_FALKORDB_GRAPH", _DEFAULT_SINGLE_TENANT_GRAPH_NAME)
