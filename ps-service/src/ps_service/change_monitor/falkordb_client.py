"""FalkorDB connection surface for `ps_service.change_monitor`.

Own copy of `ps_service.company_merge.falkordb_client`'s shape
(PLAN_REVIEWED.md §1.3 -- a deliberate near-duplicate, not a shared import;
4 precedents: ingestion / query_engine / company_merge / domain_mapper):
`GraphHandle`/`GraphQueryResult`/`_ConnectivityProbe` Protocols,
`connect`/`connect_from_config`/`check_connectivity`/`select_graph`, and
this component's own graph-naming helper.

`native_graph_name` matches Ingestion's own
`{short_name.lower()}_native` formula exactly -- `trigger_reingestion`'s
own bookkeeping writes land in the same per-regulation native graph
Ingestion wrote, so the formula must be identical, not just similar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ps_service.change_monitor.errors import ChangeMonitorConfigurationError
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.graph_gateway.read_only import ReadOnlyDatabase, open_read_only_database

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

_NATIVE_GRAPH_SUFFIX = "_native"


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`.

    The one field any caller of `GraphHandle.query` needs to read off a
    response.
    """

    @property
    def result_set(self) -> list[object]:
        """The query's result rows, one list of column values per row."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for `falkordb.Graph`.

    The `query()` call surface this component's graph reader / succession
    logic needs: bare Cypher, or Cypher + `params=` for parameterized
    reads/writes. Callers outside this module never import `falkordb.Graph`
    directly.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


class _ConnectivityProbe(Protocol):
    """Structural requirement for `check_connectivity`'s `db` parameter.

    Only the one call surface needed to verify connectivity. A real
    `connect()`-returned `FalkorDB` instance satisfies this structurally with
    no change; tests can substitute a lightweight fake instead of mocking the
    real client type.
    """

    def list_graphs(self) -> list[str]: ...


def connect(
    host: str, port: int, *, socket_connect_timeout: float, socket_timeout: float
) -> ReadOnlyDatabase:
    """Open a read-only FalkorDB connection: every graph it selects sends `GRAPH.RO_QUERY` only.

    Does not itself verify connectivity; call `check_connectivity()` against the result to
    fail loud early.

    Args:
        host: FalkorDB host name or address.
        port: FalkorDB TCP port.
        socket_connect_timeout: Longest wait to establish the TCP connection, in seconds.
        socket_timeout: Longest wait for a reply on an open connection, in seconds.

    Raises:
        redis.exceptions.TimeoutError: The server accepted the connection but did not answer
            within a bound.
    """
    return open_read_only_database(
        host, port, socket_connect_timeout=socket_connect_timeout, socket_timeout=socket_timeout
    )


def connect_from_config(config: ServiceConfig) -> ReadOnlyDatabase:
    """Build the real FalkorDB connection from `config.falkordb_host`/`config.falkordb_port`.

    Mirrors `ps_service.company_merge.falkordb_client.connect_from_config`
    exactly. Callers build `config` via `ps_service.config.load_config()`,
    never by constructing a `ServiceConfig` with hardcoded host/port values.
    """
    return connect(
        host=config.falkordb_host,
        port=config.falkordb_port,
        socket_connect_timeout=config.falkordb_connect_timeout_seconds,
        socket_timeout=config.falkordb_socket_timeout_seconds,
    )


def check_connectivity(db: _ConnectivityProbe, host: str, port: int) -> None:
    """Fail loud, with a friendly message, before doing any real work.

    Raises `ChangeMonitorConfigurationError` (wrapping the underlying cause)
    if `db` cannot list its graphs -- the cheapest real round-trip available
    to confirm the connection is actually usable. Records the outcome in
    `ps_service.dependency_health` either way, so a caller using this as a
    startup probe also feeds the live readiness signal.
    """
    try:
        db.list_graphs()
    except Exception as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise ChangeMonitorConfigurationError(
            f"FalkorDB connection failed at {host}:{port}. Is FalkorDB running? Error: {exc}"
        ) from exc
    mark_healthy(FALKORDB)


def select_graph(db: ReadOnlyDatabase, name: str) -> GraphHandle:
    """Select graph `name` on `db` as this module's local, read-only `GraphHandle`.

    This is the single conversion site -- callers elsewhere in `ps_service.change_monitor` never see
    the driver's graph type. `name` is expected to already be the final graph name -- this
    function does no naming/namespacing of its own.
    """
    return db.select_graph(name)


def native_graph_name(short_name: str) -> str:
    """The regulation-scoped native graph Ingestion wrote to.

    `{short_name.lower()}_native` (e.g. `"CRA"` -> `"cra_native"`) --
    matches `ps_service.ingestion.falkordb_client.native_graph_name`'s
    formula exactly. `trigger_reingestion`'s own bookkeeping writes land
    here.
    """
    return f"{short_name.lower()}{_NATIVE_GRAPH_SUFFIX}"
