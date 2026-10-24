"""`ReadOnlyGraph`: every read goes to the server-side read-only call.

The driver is the only fake: a graph that records which method was invoked. `GRAPH.RO_QUERY`
refuses a write on the server, so a handle that only ever sends `ro_query` cannot mutate a graph
(that is proven live in `query_engine/test_ro_query_rejects_write_live.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from redis.exceptions import ResponseError

from graph_gateway.driver_connectors import connector_returning
from ps_service.config import load_config
from ps_service.graph_gateway.read_only import (
    ReadOnlyGraph,
    open_read_only_database,
    open_read_only_database_from_config,
)

if TYPE_CHECKING:
    from ps_service.graph_gateway.read_only import DriverDatabase, DriverGraph

_EMPTY_KEY = "Invalid graph operation on empty key"


class _Reply:
    def __init__(self, rows: list[object]) -> None:
        self.header: list[list[object]] = [[1, "c"]]
        self.result_set = rows


class _RecordingDriverGraph:
    """Driver graph that records the method names and arguments it was called with."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.methods: list[str] = []
        self.ro_calls: list[tuple[str, dict[str, object] | None, int | None]] = []
        self._error = error

    def ro_query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _Reply:
        self.methods.append("ro_query")
        self.ro_calls.append((q, params, timeout))
        if self._error is not None:
            raise self._error
        return _Reply([[3]])

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _Reply:
        self.methods.append("query")
        return _Reply([[3]])


class _DriverDatabase:
    def __init__(self, graph: _RecordingDriverGraph) -> None:
        self.graph = graph
        self.selected: list[str] = []

    def select_graph(self, name: str) -> DriverGraph:
        self.selected.append(name)
        return self.graph

    def list_graphs(self) -> list[str]:
        return ["policy_system"]


def _read_only(driver_graph: _RecordingDriverGraph) -> ReadOnlyGraph:
    database = open_read_only_database(
        "h",
        1,
        socket_connect_timeout=1.0,
        socket_timeout=2.0,
        connect=connector_returning(_DriverDatabase(driver_graph)),
    )
    return database.select_graph("policy_system")


def test_query_goes_to_ro_query_and_never_to_query() -> None:
    driver_graph = _RecordingDriverGraph()

    rows = _read_only(driver_graph).query("MATCH (n) RETURN count(n)").result_set

    assert driver_graph.methods == ["ro_query"]
    assert rows == [[3]]


def test_params_and_timeout_are_forwarded_unchanged() -> None:
    driver_graph = _RecordingDriverGraph()

    _read_only(driver_graph).query("MATCH (n {id: $id}) RETURN n", {"id": "x"}, 1500)

    assert driver_graph.ro_calls == [("MATCH (n {id: $id}) RETURN n", {"id": "x"}, 1500)]


def test_open_read_only_database_selects_graphs_that_are_read_only() -> None:
    driver_graph = _RecordingDriverGraph()
    driver_database = _DriverDatabase(driver_graph)
    seen: list[tuple[str, int, float, float]] = []

    def _connect(
        host: str, port: int, *, socket_connect_timeout: float, socket_timeout: float
    ) -> DriverDatabase:
        seen.append((host, port, socket_connect_timeout, socket_timeout))
        return driver_database

    database = open_read_only_database(
        "db.example", 7000, socket_connect_timeout=3.0, socket_timeout=9.0, connect=_connect
    )
    graph = database.select_graph("cra_native")
    graph.query("RETURN 1")

    assert seen == [("db.example", 7000, 3.0, 9.0)]
    assert driver_database.selected == ["cra_native"]
    assert database.list_graphs() == ["policy_system"]
    assert driver_graph.methods == ["ro_query"]


def test_open_read_only_database_from_config_uses_the_configured_host_port_and_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PS_FALKORDB_HOST", "10.20.30.40")
    monkeypatch.setenv("PS_FALKORDB_PORT", "7000")
    monkeypatch.setenv("PS_FALKORDB_CONNECT_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("PS_FALKORDB_SOCKET_TIMEOUT_SECONDS", "40")
    seen: list[tuple[str, int, float, float]] = []

    def _connect(
        host: str, port: int, *, socket_connect_timeout: float, socket_timeout: float
    ) -> DriverDatabase:
        seen.append((host, port, socket_connect_timeout, socket_timeout))
        return _DriverDatabase(_RecordingDriverGraph())

    open_read_only_database_from_config(load_config(), connect=_connect)

    assert seen == [("10.20.30.40", 7000, 7.5, 40.0)]


def test_a_write_the_server_rejects_surfaces_the_server_error_unchanged() -> None:
    rejection = ResponseError("graph.RO_QUERY is to be executed only on read-only queries")
    driver_graph = _RecordingDriverGraph(error=rejection)

    with pytest.raises(ResponseError) as raised:
        _read_only(driver_graph).query("CALL db.idx.fulltext.createNodeIndex('Spike','name')")

    assert raised.value is rejection


def test_a_missing_graph_key_reads_as_an_empty_result() -> None:
    driver_graph = _RecordingDriverGraph(error=ResponseError(_EMPTY_KEY))

    reply = _read_only(driver_graph).query("MATCH (n) RETURN count(n) AS c LIMIT 1")

    assert reply.header == []
    assert reply.result_set == []


def test_no_other_server_error_is_mapped_to_an_empty_result() -> None:
    other = ResponseError("Query timed out")
    driver_graph = _RecordingDriverGraph(error=other)

    with pytest.raises(ResponseError) as raised:
        _read_only(driver_graph).query("MATCH (n) RETURN n", None, 1)

    assert raised.value is other
