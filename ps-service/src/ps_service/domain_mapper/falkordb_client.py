"""Read-only FalkorDB connection surface for `ps_service.domain_mapper`.

The Domain Mapper writes nothing through this surface: its baseline-graph writes are Graph
Write Gateway groups (`graph_writer`), and the handles built here only READ (the native graph,
and the baseline graph's Requirements and existing nodes).

This module keeps its own copy of the shape in `ps_service.ingestion.falkordb_client` so the
components stay independently readable and testable (a deliberate near-duplicate, not a shared
import): the `GraphHandle` and `GraphQueryResult` Protocols, `connect`, `connect_from_config`,
`select_graph`, and this component's graph-naming helpers.

Naming: `native_graph_name` matches Ingestion's `{short_name.lower()}_native` convention
exactly, because the Domain Mapper reads the native graph Ingestion wrote. `baseline_graph_name`
is this component's own per-regulation graph (`{short_name.lower()}_baseline`), written through
the gateway and read through a handle from here, never the company's merged single-tenant graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.domain_mapper.errors import DomainMapperConfigurationError
from ps_service.graph_gateway.read_only import ReadOnlyDatabase, open_read_only_database

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_NATIVE_GRAPH_SUFFIX = "_native"
_BASELINE_GRAPH_SUFFIX = "_baseline"


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`.

    Exposes the one field any caller of `GraphHandle.query` reads from a response.
    """

    @property
    def result_set(self) -> list[object]:
        """Return the rows of the query; empty when nothing matched."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`.

    The `query()` call surface this component's adapters and derivation logic need: bare
    Cypher, or Cypher + `params=` for parameterized reads (the Domain Mapper issues no write
    through a handle). Callers outside this module never import `falkordb.Graph` directly.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run read-only Cypher `q`, bound to `params` when given, and return the result.

        Args:
            q: Cypher text; must not write.
            params: Query parameters referenced as `$name` in `q`; `None` for none.

        Returns:
            The query result, whose `result_set` holds the matched rows.
        """
        ...


class _ConnectivityProbe(Protocol):
    """Structural requirement for `check_connectivity`'s `db` parameter.

    The one call needed to verify connectivity. A real `connect()` result satisfies it
    structurally, and a lightweight fake can stand in for it.
    """

    def list_graphs(self) -> list[str]: ...


def connect(host: str, port: int) -> ReadOnlyDatabase:
    """Open a read-only FalkorDB connection: every graph it selects sends `GRAPH.RO_QUERY` only.

    Does not itself verify connectivity; call `check_connectivity()` against the result to
    fail loud early.

    Args:
        host: FalkorDB host name or address.
        port: FalkorDB TCP port.

    Returns:
        The read-only database handle.
    """
    return open_read_only_database(host, port)


def connect_from_config(config: ServiceConfig) -> ReadOnlyDatabase:
    """Build the read-only FalkorDB connection from `config`'s host and port.

    Mirrors `ps_service.ingestion.falkordb_client.connect_from_config`. Callers obtain `config`
    from `ps_service.config.load_config()` rather than hardcoding host and port.

    Args:
        config: The resolved service configuration.

    Returns:
        The read-only database handle; connectivity is not verified.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def check_connectivity(db: _ConnectivityProbe, host: str, port: int) -> None:
    """Fail loud, with a friendly message, before doing any real work.

    Listing the graphs is the cheapest real round-trip that confirms the connection is usable.

    Args:
        db: Connection to probe.
        host: Host name used only in the failure message.
        port: Port used only in the failure message.

    Raises:
        DomainMapperConfigurationError: If `db` cannot list its graphs; the cause is chained.

    Side Effects:
        Records the FalkorDB outcome (healthy or unhealthy) in `ps_service.dependency_health`
        either way, so a caller using this as a startup probe also feeds the live readiness
        signal.
    """
    try:
        db.list_graphs()
    except Exception as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise DomainMapperConfigurationError(
            f"FalkorDB connection failed at {host}:{port}. Is FalkorDB running? Error: {exc}"
        ) from exc
    mark_healthy(FALKORDB)


def select_graph(db: ReadOnlyDatabase, name: str) -> GraphHandle:
    """Select graph `name` on `db` as this module's local, read-only `GraphHandle`.

    This is the single conversion site: callers elsewhere in `ps_service.domain_mapper` never see
    the driver's graph type.

    Args:
        db: Read-only database returned by `connect`.
        name: Final graph name; no naming or namespacing is applied here.

    Returns:
        A read-only handle on graph `name`.
    """
    return db.select_graph(name)


def native_graph_name(short_name: str) -> str:
    """Return the name of the regulation's native graph, which Ingestion wrote.

    The name is `{short_name.lower()}_native` (e.g. `"CRA"` -> `"cra_native"`). The Domain
    Mapper only reads this graph.

    Args:
        short_name: The regulation's short name, in any case.

    Returns:
        The lower-cased native graph name.
    """
    return f"{short_name.lower()}{_NATIVE_GRAPH_SUFFIX}"


def baseline_graph_name(short_name: str) -> str:
    """Return the name of the per-regulation graph holding this component's own output.

    The name is `{short_name.lower()}_baseline` (e.g. `"CRA"` -> `"cra_baseline"`), distinct
    from the native graph and from the company's merged single-tenant graph, so one
    regulation's baseline stays isolated from every other.

    Args:
        short_name: The regulation's short name, in any case.

    Returns:
        The lower-cased baseline graph name.
    """
    return f"{short_name.lower()}{_BASELINE_GRAPH_SUFFIX}"
