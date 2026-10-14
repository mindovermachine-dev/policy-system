"""FalkorDB connection surface for `ps_service.query_engine`.

This module imports no driver: it delegates to the gateway's read-only handle
(`ps_service.graph_gateway.read_only`), so every Query Engine query is sent as `GRAPH.RO_QUERY`
and the FalkorDB server, not a textual guard, refuses a write. Its public names are
`GraphHandle`, `GraphQueryResult`, `connect`, `connect_from_config` and `select_graph`.

`GraphQueryResult` exposes `header` as well as `result_set`, because `execute_cypher_query`
derives column names from `result.header` (`[type, name]` pairs). There is deliberately no
separate connectivity probe: the driver constructor itself issues a Redis round-trip, so
construction fails fast when the server is unreachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_gateway.read_only import (
    ReadOnlyDatabase,
    ReadOnlyGraph,
    ReadOnlyQueryResult,
    open_read_only_database,
)

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig

__all__ = [
    "GraphHandle",
    "GraphQueryResult",
    "connect",
    "connect_from_config",
    "select_graph",
]

GraphQueryResult = ReadOnlyQueryResult
"""Alias of the gateway's `ReadOnlyQueryResult` (`header` and `result_set`)."""

GraphHandle = ReadOnlyGraph
"""Alias of the gateway's `ReadOnlyGraph`: the `query()` surface `execute_cypher_query` needs."""


def connect(host: str, port: int) -> ReadOnlyDatabase:
    """Open a read-only FalkorDB connection for `host`/`port`.

    The driver constructor issues a Redis round-trip (sentinel/cluster detection), so it fails
    fast if the server is unreachable; this function adds no further explicit probe.

    Args:
        host: FalkorDB server host name or address.
        port: FalkorDB server TCP port.

    Returns:
        A connection whose selected graphs run only read queries.

    Raises:
        redis.exceptions.RedisError: the server is unreachable (propagated from the driver).

    Side Effects:
        Opens a network connection to the server.
    """
    return open_read_only_database(host, port)


def connect_from_config(config: ServiceConfig) -> ReadOnlyDatabase:
    """Open a read-only connection from `config.falkordb_host` / `config.falkordb_port`.

    Callers build `config` via `ps_service.config.load_config()`, never with hardcoded values.

    Args:
        config: Service configuration naming the FalkorDB host and port.

    Returns:
        A connection whose selected graphs run only read queries.

    Raises:
        redis.exceptions.RedisError: the server is unreachable (propagated from the driver).

    Side Effects:
        Opens a network connection to the server.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def select_graph(db: ReadOnlyDatabase, name: str) -> GraphHandle:
    """Select graph `name` on `db`; every query on the returned handle is read-only.

    Args:
        db: A connection from `connect` or `connect_from_config`.
        name: Name of the graph to select.

    Returns:
        A handle whose `query` sends only read queries.
    """
    return db.select_graph(name)
