# © 2026 Cartman ApS. All rights reserved.
"""FalkorDB connection helper for `ps_service.ingestion`.

Ingestion reads the graph (existence reads, reachability and pre-flight counts) and writes only
through the Graph Write Gateway. This module imports no driver: `connect` returns the gateway's
read-only database (`graph_gateway.read_only`), so every query is sent as `GRAPH.RO_QUERY`, and
the connectivity probe comes from `graph_gateway.connectivity`.

`FalkorDBConnectionError` subclasses `IngestionConfigurationError`
(`ps_service.ingestion.errors`), the type raised when Ingestion's FalkorDB connection cannot be
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
    """FalkorDB is unreachable when `check_connectivity` probes it.

    Subclasses `IngestionConfigurationError`, so a caller catching that family also catches this.
    The original probe failure is chained as `__cause__`.
    """


def connect(
    host: str, port: int, *, socket_connect_timeout: float, socket_timeout: float
) -> ReadOnlyDatabase:
    """Open a read-only FalkorDB connection (see `graph_gateway.read_only`).

    The driver connects eagerly, so call this only inside a boundary that records dependency
    health, such as `check_connectivity_from_config`.

    Args:
        host: FalkorDB host name or address.
        port: FalkorDB TCP port.
        socket_connect_timeout: Longest wait to establish the TCP connection, in seconds.
        socket_timeout: Longest wait for a reply on an open connection, in seconds.

    Returns:
        A `ReadOnlyDatabase` whose graphs accept only read queries.

    Side Effects:
        Opens a network connection to FalkorDB.
    """
    return open_read_only_database(
        host, port, socket_connect_timeout=socket_connect_timeout, socket_timeout=socket_timeout
    )


def connect_from_config(config: ServiceConfig) -> ReadOnlyDatabase:
    """Open the read-only connection to the host and port held by a resolved `ServiceConfig`.

    Args:
        config: Configuration from `ps_service.config.load_config()`; its `falkordb_host` and
            `falkordb_port` (from `PS_FALKORDB_HOST` / `PS_FALKORDB_PORT`) select the target.

    Returns:
        A `ReadOnlyDatabase` as returned by `connect`.

    Side Effects:
        Opens a network connection to FalkorDB.
    """
    return connect(
        host=config.falkordb_host,
        port=config.falkordb_port,
        socket_connect_timeout=config.falkordb_connect_timeout_seconds,
        socket_timeout=config.falkordb_socket_timeout_seconds,
    )


def check_connectivity(db: ConnectivityProbe, host: str, port: int) -> None:
    """Verify that FalkorDB is reachable, failing with a clear message before real work starts.

    Args:
        db: An open connection able to list its graphs.
        host: Host name used only in the failure message.
        port: Port used only in the failure message.

    Raises:
        FalkorDBConnectionError: `db` cannot list its graphs (the cause is chained).
    """
    try:
        gateway_connectivity.check_connectivity(db, host, port)
    except GraphConnectivityError as exc:
        raise FalkorDBConnectionError(str(exc)) from exc.__cause__


def check_connectivity_from_config(config: ServiceConfig) -> None:
    """Connect via this module's `connect_from_config` and verify connectivity, as one guarded step.

    Args:
        config: Resolved configuration naming the FalkorDB host and port.

    Side Effects:
        Opens a network connection to FalkorDB and lists its graphs.

    Raises:
        FalkorDBConnectionError: the connection or the probe failed (the cause is chained).
    """
    try:
        gateway_connectivity.check_connectivity_via(config, connect_from_config)
    except GraphConnectivityError as exc:
        raise FalkorDBConnectionError(str(exc)) from exc.__cause__


def select_graph(db: ReadOnlyDatabase, name: str) -> GraphHandle:
    """Select graph `name` on `db`.

    Args:
        db: An open read-only connection.
        name: Final graph name, used as given (see `native_graph_name`).

    Returns:
        A read-only handle exposing `query()` for that graph.
    """
    return db.select_graph(name)


def native_graph_name(short_name: str) -> str:
    """Return the name of the native structural graph for a regulation.

    Args:
        short_name: The instrument's short name, in any case.

    Returns:
        `{short_name.lower()}_native`; there is one such graph per regulation.
    """
    return f"{short_name.lower()}{_NATIVE_GRAPH_SUFFIX}"
