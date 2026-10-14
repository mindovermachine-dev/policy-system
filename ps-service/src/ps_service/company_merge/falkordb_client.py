"""FalkorDB connection surface for `ps_service.company_merge`.

Holds this component's own copy of the connection helpers in
`ps_service.domain_mapper.falkordb_client`: the `GraphHandle` and `GraphQueryResult` protocols,
`connect`, `connect_from_config`, `check_connectivity`, `select_graph`, and the graph-name
accessor `single_tenant_graph_name`. The copy is deliberate; it is not a shared import.

`single_tenant_graph_name()` follows the same `PS_FALKORDB_GRAPH` convention (default
`"policy_system"`) as `ps_service.mcp_interface.mcp_server`: Company Merge reads the one
single-tenant graph that `mcp_interface` exposes for read-only Cypher.

The surface is read-only. Every Company Merge write is a Graph Write Gateway group
(`graph_submission.submit_group`), and no write Cypher is handed to `GraphHandle.query`. The
connection is the gateway's `ReadOnlyDatabase` (`graph_gateway.read_only`): every query is
sent as `GRAPH.RO_QUERY`, so the FalkorDB server refuses a write, and this module imports no
driver.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from ps_service.company_merge.errors import CompanyMergeConfigurationError
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.graph_gateway.read_only import ReadOnlyDatabase, open_read_only_database

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_DEFAULT_SINGLE_TENANT_GRAPH_NAME = "policy_system"


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`.

    Exposes the one field any caller of `GraphHandle.query` reads from a response.
    """

    @property
    def result_set(self) -> list[object]:
        """The query's result rows, one list of column values per row."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`.

    Carries the `query()` call surface the graph reader, dedup and redirect logic need: bare
    Cypher, or Cypher plus `params` for parameterized reads. Callers outside this module never
    import `falkordb.Graph`. A `ReadOnlyGraph` satisfies it structurally.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run read-only Cypher `q` and return the result.

        Args:
            q: Cypher text; a write clause is refused by the read-only connection.
            params: Query parameters bound to `$name` placeholders, or None for none.

        Returns:
            The query result; `result_set` is empty when nothing matched.
        """
        ...


class _ConnectivityProbe(Protocol):
    """Structural requirement for `check_connectivity`'s `db` parameter.

    Names only the call needed to verify connectivity, so a `connect()` result satisfies it and
    a lightweight fake can stand in without mocking the real client type.
    """

    def list_graphs(self) -> list[str]: ...


def connect(host: str, port: int) -> ReadOnlyDatabase:
    """Open a read-only FalkorDB connection whose graphs send `GRAPH.RO_QUERY` only.

    Does not verify connectivity; call `check_connectivity()` on the result to fail early.

    Args:
        host: FalkorDB host name or address.
        port: FalkorDB TCP port.

    Returns:
        A read-only database handle.
    """
    return open_read_only_database(host, port)


def connect_from_config(config: ServiceConfig) -> ReadOnlyDatabase:
    """Build the real FalkorDB connection from `config.falkordb_host`/`config.falkordb_port`.

    Behaves like `ps_service.domain_mapper.falkordb_client.connect_from_config`. Callers build
    `config` with `ps_service.config.load_config()` rather than hardcoding host and port.

    Args:
        config: Service configuration supplying `falkordb_host` and `falkordb_port`.

    Returns:
        A read-only database handle, as returned by `connect`.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def check_connectivity(db: _ConnectivityProbe, host: str, port: int) -> None:
    """Verify the FalkorDB connection works, failing early with a readable message.

    Lists the graphs on `db`, the cheapest real round-trip that proves the connection is
    usable. Records the outcome in `ps_service.dependency_health` either way, so a startup
    probe also feeds the live readiness signal.

    Args:
        db: Connection to probe.
        host: Host name used only in the failure message.
        port: Port used only in the failure message.

    Raises:
        CompanyMergeConfigurationError: `db` cannot list its graphs; the underlying cause is
            chained.
    """
    try:
        db.list_graphs()
    except Exception as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise CompanyMergeConfigurationError(
            f"FalkorDB connection failed at {host}:{port}. Is FalkorDB running? Error: {exc}"
        ) from exc
    mark_healthy(FALKORDB)


def select_graph(db: ReadOnlyDatabase, name: str) -> GraphHandle:
    """Select graph `name` on `db` as this module's local, read-only `GraphHandle`.

    This is the single conversion site, so the rest of `ps_service.company_merge` never sees
    the driver's graph type. It applies no naming or namespacing of its own.

    Args:
        db: Read-only connection to select from.
        name: Final graph name, such as `single_tenant_graph_name()` or a `{short}_baseline`
            name built by the caller.

    Returns:
        A read-only handle on graph `name`.
    """
    return db.select_graph(name)


def single_tenant_graph_name() -> str:
    """Return the name of the company's merged single-tenant regulatory graph.

    This is the one graph name shared across the system for read-only query access and for
    the gateway's writes, and it follows the `DEFAULT_GRAPH` convention of
    `ps_service.mcp_interface.mcp_server`.

    Returns:
        The value of the `PS_FALKORDB_GRAPH` environment variable, or `"policy_system"` when
        it is unset.
    """
    return os.environ.get("PS_FALKORDB_GRAPH", _DEFAULT_SINGLE_TENANT_GRAPH_NAME)
