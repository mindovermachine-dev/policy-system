"""FalkorDB connection surface for `ps_service.query_engine`.

Since issue #215 this module imports no driver: it delegates to the gateway's read-only handle
(`ps_service.graph_gateway.read_only`), so every Query Engine query is sent as `GRAPH.RO_QUERY`
and the FalkorDB server, not a textual guard, refuses a write. It keeps its public names
(`GraphHandle`, `GraphQueryResult`, `connect`, `connect_from_config`, `select_graph`) so the MCP
Interface and the tests that patch them are unchanged.

`GraphQueryResult` exposes `header` as well as `result_set`, because `execute_cypher_query`
derives column names from `result.header` (`[type, name]` pairs). There is deliberately no
connectivity probe here: no AC requires one for the Query Engine, and the driver constructor
itself issues a Redis round-trip, so construction fails fast when the server is unreachable.
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
    """
    return open_read_only_database(host, port)


def connect_from_config(config: ServiceConfig) -> ReadOnlyDatabase:
    """Open a read-only connection from `config.falkordb_host` / `config.falkordb_port`.

    Callers build `config` via `ps_service.config.load_config()`, never with hardcoded values.
    """
    return connect(host=config.falkordb_host, port=config.falkordb_port)


def select_graph(db: ReadOnlyDatabase, name: str) -> GraphHandle:
    """Select graph `name` on `db`; every query on the returned handle is read-only."""
    return db.select_graph(name)
