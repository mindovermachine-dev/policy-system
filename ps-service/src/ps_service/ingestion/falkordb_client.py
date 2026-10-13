# © 2026 Cartman ApS. All rights reserved.
"""FalkorDB connection helper for `ps_service.ingestion`.

Ingestion reads the graph (existence reads, reachability and pre-flight counts) and writes only
through the Graph Write Gateway. Since issue #215 this module imports no driver: `connect` hands
back the gateway's read-only database (`graph_gateway.read_only`), so every query is sent as
`GRAPH.RO_QUERY`, and the connectivity probe comes from `graph_gateway.connectivity`. It keeps its
public names (`connect`, `connect_from_config`, `select_graph`, `GraphHandle`, `GraphQueryResult`,
`FalkorDBConnectionError`, `check_connectivity*`, `native_graph_name`) so callers and their test
patch sites are unchanged.

`FalkorDBConnectionError` subclasses `IngestionConfigurationError` (`ps_service.ingestion.errors`),
whose docstring documents it as the type raised when Ingestion's FalkorDB connection cannot be
established.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ps_service.graph_gateway import connectivity as gateway_connectivity
from ps_service.graph_gateway.connectivity import ConnectivityProbe, GraphConnectivityError
from ps_service.graph_gateway.read_only import ReadOnlyDatabase, open_read_only_database
from ps_service.ingestion.errors import IngestionConfigurationError

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

__all__ = [
    "FalkorDBConnectionError",
    "GraphHandle",
    "GraphQueryResult",
    "check_connectivity",
    "check_connectivity_from_config",
    "connect",
    "connect_from_config",
    "native_graph_name",
    "select_graph",
]

_NATIVE_GRAPH_SUFFIX = "_native"


class GraphQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`: the rows a query returned."""

    @property
    def result_set(self) -> list[object]:
        """The rows returned by the query."""
        ...


class GraphHandle(Protocol):
    """The `query()` surface Ingestion's reads need: bare Cypher, or Cypher with `params=`.

    A `ReadOnlyGraph` satisfies it structurally, so every handle opened here is read-only.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


class FalkorDBConnectionError(IngestionConfigurationError):
    """FalkorDB is unreachable at `check_connectivity` time.

    Subclasses `IngestionConfigurationError`, catchable by any caller relying on that family.
    """


def connect(host: str, port: int) -> ReadOnlyDatabase:
    """Open a read-only FalkorDB connection (see `graph_gateway.read_only`).

    Do not call this outside a `dependency_health`-recording boundary
    (`check_connectivity_from_config`): the driver connects eagerly.
    """
    return open_read_only_database(host, port)


def connect_from_config(config: ServiceConfig) -> ReadOnlyDatabase:
    """Open the read-only connection from a resolved `ServiceConfig`.

    Callers build `config` via `ps_service.config.load_config()`, which is what makes
    `PS_FALKORDB_HOST` / `PS_FALKORDB_PORT` load-bearing.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def check_connectivity(db: ConnectivityProbe, host: str, port: int) -> None:
    """Fail loud, with a friendly message, before doing any real work.

    Raises:
        FalkorDBConnectionError: `db` cannot list its graphs (the cause is chained).
    """
    try:
        gateway_connectivity.check_connectivity(db, host, port)
    except GraphConnectivityError as exc:
        raise FalkorDBConnectionError(str(exc)) from exc.__cause__


def check_connectivity_from_config(config: ServiceConfig) -> None:
    """Connect via this module's `connect_from_config` and verify connectivity, as one guarded step.

    Raises:
        FalkorDBConnectionError: the connection or the probe failed (the cause is chained).
    """
    try:
        gateway_connectivity.check_connectivity_via(config, connect_from_config)
    except GraphConnectivityError as exc:
        raise FalkorDBConnectionError(str(exc)) from exc.__cause__


def select_graph(db: ReadOnlyDatabase, name: str) -> GraphHandle:
    """Select graph `name` on `db`; `name` is already the final graph name."""
    return db.select_graph(name)


def native_graph_name(short_name: str) -> str:
    """One FalkorDB graph name per regulation, `{short_name.lower()}_native`."""
    return f"{short_name.lower()}{_NATIVE_GRAPH_SUFFIX}"
