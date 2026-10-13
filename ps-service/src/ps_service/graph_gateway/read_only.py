# © 2026 Cartman ApS. All rights reserved.
"""`ReadOnlyGraph`: the handle every reader outside the gateway uses (issue #215).

A `ReadOnlyGraph` sends each query as `GRAPH.RO_QUERY`, which the FalkorDB server refuses to run if
it would write. That makes the server, not a textual guard, the boundary for reads. Its `query`
keeps the call shape of the writable handle (`q`, `params`, `timeout`), so a reader changes its
handle type and nothing else.

One reply differs from `GRAPH.QUERY`: `GRAPH.RO_QUERY` on a graph key that does not exist raises
`Invalid graph operation on empty key`, where `GRAPH.QUERY` answers an aggregate with `[[0]]` and
creates the key. That exact reply is mapped to an empty result here (no header, no rows) so a
reader sees "no data" instead of an error; every other server error passes through unchanged.
Readers that index an aggregate row must treat the empty result as zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from redis.exceptions import ResponseError

from ps_service.graph_gateway import connection as driver_connection

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.config import ServiceConfig

__all__ = [
    "DriverDatabase",
    "DriverGraph",
    "ReadOnlyDatabase",
    "ReadOnlyGraph",
    "ReadOnlyQueryResult",
    "open_read_only_database",
    "open_read_only_database_from_config",
]

_EMPTY_KEY_REPLY = "Invalid graph operation on empty key"


class ReadOnlyQueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult`: column header and rows."""

    @property
    def header(self) -> list[list[object]]:
        """`[type, name]` pairs, one per result column."""
        ...

    @property
    def result_set(self) -> list[object]:
        """The rows exactly as the driver returned them."""
        ...


class ReadOnlyGraph(Protocol):
    """A graph that can only be read: every query goes to the server's read-only call."""

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> ReadOnlyQueryResult:
        """Run Cypher `q` with optional `params` and `timeout` (ms); return the raw result.

        Raises:
            redis.exceptions.ResponseError: the server rejected the query (a write, a timeout,
                a syntax error); the server's own text is kept.
        """
        ...


class ReadOnlyDatabase(Protocol):
    """A FalkorDB connection that hands out read-only graphs."""

    def select_graph(self, name: str, /) -> ReadOnlyGraph:
        """Select graph `name` for reading (positional, so a real `FalkorDB` satisfies this)."""
        ...

    def list_graphs(self) -> list[str]:
        """Names of the graphs on the server."""
        ...


class DriverGraph(Protocol):
    """The one driver graph method this module uses: `Graph.ro_query`."""

    def ro_query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> ReadOnlyQueryResult:
        """Run `q` through `GRAPH.RO_QUERY`."""
        ...


class DriverDatabase(Protocol):
    """The driver connection surface this module uses; a real `FalkorDB` satisfies it."""

    def select_graph(self, graph_id: str, /) -> DriverGraph:
        """Select a driver graph."""
        ...

    def list_graphs(self) -> list[str]:
        """Names of the graphs on the server."""
        ...


class _EmptyResult:
    """The answer to a read of a graph key that does not exist: no columns, no rows."""

    @property
    def header(self) -> list[list[object]]:
        return []

    @property
    def result_set(self) -> list[object]:
        return []


class _RoQueryGraph:
    """`ReadOnlyGraph` over a driver graph: `query` is always `ro_query`."""

    def __init__(self, graph: DriverGraph) -> None:
        self._graph = graph

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> ReadOnlyQueryResult:
        try:
            return self._graph.ro_query(q, params, timeout)
        except ResponseError as exc:
            if _EMPTY_KEY_REPLY in str(exc):
                return _EmptyResult()
            raise


class _RoDatabase:
    """`ReadOnlyDatabase` over a driver connection."""

    def __init__(self, database: DriverDatabase) -> None:
        self._database = database

    def select_graph(self, name: str) -> ReadOnlyGraph:
        return _RoQueryGraph(self._database.select_graph(name))

    def list_graphs(self) -> list[str]:
        return self._database.list_graphs()


def open_read_only_database(
    host: str,
    port: int,
    *,
    connect: Callable[[str, int], DriverDatabase] | None = None,
) -> ReadOnlyDatabase:
    """Connect to FalkorDB at `host`/`port`; every graph it selects is read-only.

    `connect` is the driver seam (tests inject a fake driver connection); by default the
    gateway's `connection.connect`, looked up at call time.
    """
    opener = connect if connect is not None else driver_connection.connect
    return _RoDatabase(opener(host, port))


def open_read_only_database_from_config(
    config: ServiceConfig,
    *,
    connect: Callable[[str, int], DriverDatabase] | None = None,
) -> ReadOnlyDatabase:
    """Connect using `config.falkordb_host` / `config.falkordb_port`; graphs are read-only."""
    return open_read_only_database(config.falkordb_host, config.falkordb_port, connect=connect)
