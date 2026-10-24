"""Shared checks for the per-package read-only client tests.

Each package that reads the graph (`company_merge`, `ingestion`, `domain_mapper`,
`change_monitor`, `export`) must reach FalkorDB only through the gateway's `ReadOnlyGraph`. The
package tests share three things: an AST check that the package imports no driver and builds no
client, a driver stand-in that records which graph method was called, and a driver graph that
answers like a missing graph key (`Invalid graph operation on empty key`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from redis.exceptions import ResponseError

from graph_gateway.write_path_scan import scan_source
from ps_service.graph_gateway import connection as driver_connection

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

__all__ = [
    "EmptyKeyDriverGraph",
    "RecordingDriverDatabase",
    "RecordingDriverGraph",
    "driver_findings",
    "install_driver",
]


def driver_findings(package_dir: Path) -> list[str]:
    """Every driver import, `FalkorDB(...)` call or write-capable factory import in the package."""
    return sorted(
        f"{path.name}: {finding}"
        for path in package_dir.rglob("*.py")
        for finding in scan_source(path.read_text(encoding="utf-8"))
        if "write Cypher" not in finding
    )


class _Reply:
    def __init__(self) -> None:
        self.header: list[list[object]] = []
        self.result_set: list[object] = []


class RecordingDriverGraph:
    """A driver graph that records the name of every query method it is asked to run."""

    def __init__(self) -> None:
        self.methods: list[str] = []

    def ro_query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _Reply:
        self.methods.append("ro_query")
        return _Reply()

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _Reply:
        self.methods.append("query")
        return _Reply()


class EmptyKeyDriverGraph:
    """A driver graph whose key does not exist: `ro_query` raises the server's empty-key reply."""

    def ro_query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _Reply:
        raise ResponseError("Invalid graph operation on empty key")


class RecordingDriverDatabase:
    """A driver connection that hands out one driver graph for every name."""

    def __init__(self, graph: RecordingDriverGraph | EmptyKeyDriverGraph) -> None:
        self._graph = graph

    def select_graph(self, name: str) -> RecordingDriverGraph | EmptyKeyDriverGraph:
        return self._graph

    def list_graphs(self) -> list[str]:
        return []


def install_driver(
    monkeypatch: pytest.MonkeyPatch, graph: RecordingDriverGraph | EmptyKeyDriverGraph
) -> None:
    """Replace the gateway's driver connect so every package opener reaches `graph` instead."""
    database = RecordingDriverDatabase(graph)

    def _connect(
        host: str, port: int, *, socket_connect_timeout: float, socket_timeout: float
    ) -> RecordingDriverDatabase:
        return database

    monkeypatch.setattr(driver_connection, "connect", _connect)
