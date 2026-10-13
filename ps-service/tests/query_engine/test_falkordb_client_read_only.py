"""The Query Engine reads through the gateway's `ReadOnlyGraph` and imports no driver (#215, S2)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import ps_service.query_engine as query_engine_package
from ps_service.graph_gateway.read_only import open_read_only_database
from ps_service.query_engine import falkordb_client

if TYPE_CHECKING:
    from ps_service.graph_gateway.read_only import DriverGraph

_ROOT = Path(query_engine_package.__file__).parent
_DRIVER_PREFIXES = ("falkordb", "redis")


class _Reply:
    def __init__(self) -> None:
        self.header: list[list[object]] = []
        self.result_set: list[object] = []


class _Graph:
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


class _Database:
    def __init__(self, graph: _Graph) -> None:
        self._graph = graph

    def select_graph(self, name: str) -> DriverGraph:
        return self._graph

    def list_graphs(self) -> list[str]:
        return []


def test_no_query_engine_module_imports_the_driver() -> None:
    offenders = sorted(
        f"{path.name}: {module}"
        for path in _ROOT.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        for module in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
        if module.startswith(_DRIVER_PREFIXES) and not module.startswith("redis.exceptions")
    )

    assert offenders == []


def test_the_public_names_are_still_there() -> None:
    for name in (
        "GraphHandle",
        "GraphQueryResult",
        "connect",
        "connect_from_config",
        "select_graph",
    ):
        assert hasattr(falkordb_client, name), name


def test_a_selected_graph_sends_only_ro_query_to_the_driver() -> None:
    graph = _Graph()
    database = open_read_only_database("h", 1, connect=lambda _host, _port: _Database(graph))

    falkordb_client.select_graph(database, "policy_system").query("MATCH (n) RETURN n")

    assert graph.methods == ["ro_query"]
