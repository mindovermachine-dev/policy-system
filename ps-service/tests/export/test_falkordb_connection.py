"""Tests for `ps_service.export.falkordb_connection` (PLAN.md Slice 2.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.export.falkordb_connection import (
    graph_copy_handle,
    graph_query_handle,
    raw_connection,
)

if TYPE_CHECKING:
    from falkordb import FalkorDB

    # Cross-module import of these module-private Protocols is deliberate here, mirroring
    # CHANGES.md B1's own documented convention ("ps_service.restore imports both Protocols
    # from ps_service.export.falkordb_connection") -- this test proves the exact same shape.
    from ps_service.export.falkordb_connection import (
        _GraphCopyHandle,  # pyright: ignore[reportPrivateUsage]
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
        _RawGraphConnection,  # pyright: ignore[reportPrivateUsage]
        _WatchablePipeline,  # pyright: ignore[reportPrivateUsage]
    )


class _FakePipeline:
    """Hand-written fake -- proves `_WatchablePipeline`'s shape is satisfiable structurally."""

    def watch(self, *names: str) -> None:
        pass

    def multi(self) -> None:
        pass

    def rename(self, src: str, dst: str) -> object:
        return True

    def execute(self) -> list[object]:
        return [True]

    def reset(self) -> None:
        pass


class _FakeConnection:
    """Hand-written fake -- proves `_RawGraphConnection`'s shape is satisfiable structurally."""

    def rename(self, src: str, dst: str) -> bool:
        return True

    def delete(self, *names: str) -> int:
        return len(names)

    def pipeline(self, *, transaction: bool = True) -> _WatchablePipeline:
        return _FakePipeline()


class _FakeGraph:
    """Hand-written fake -- proves `_GraphCopyHandle`'s shape is satisfiable structurally."""

    def copy(self, clone: str) -> object:
        return None

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        return None


class _FakeGraphQueryHandle:
    """Hand-written fake -- proves `_GraphQueryHandle`'s shape is satisfiable structurally."""

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        return None


class _FakeFalkorDB:
    """Structurally stands in for `falkordb.FalkorDB` -- only the members this module touches."""

    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def select_graph(self, graph_id: str) -> _FakeGraph:
        return _FakeGraph()


def test_fake_connection_satisfies_raw_graph_connection_protocol_shape() -> None:
    """Static-shape proof (PLAN.md Slice 2.1, CHANGES2.md §3.2 amendment).

    Fails `basedpyright`, not this assert, if wrong.
    """
    connection: _RawGraphConnection = _FakeConnection()

    assert connection.rename("src", "dst") is True
    assert connection.delete("a", "b") == 2


def test_fake_pipeline_satisfies_watchable_pipeline_protocol_shape() -> None:
    """Static-shape proof (CHANGES.md B1): fails `basedpyright`, not this assertion, if wrong."""
    pipe: _WatchablePipeline = _FakePipeline()

    pipe.watch("watched_key")
    pipe.multi()
    pipe.rename("src", "dst")
    assert pipe.execute() == [True]
    pipe.reset()


def test_fake_graph_satisfies_graph_copy_handle_protocol_shape() -> None:
    """Static-shape proof: fails `basedpyright`, not this assertion, if wrong."""
    graph: _GraphCopyHandle = _FakeGraph()

    graph.copy("clone_name")
    graph.query("MATCH (n) RETURN n")


def test_raw_connection_returns_db_connection_unchanged() -> None:
    fake_connection = _FakeConnection()
    db = _FakeFalkorDB(fake_connection)

    result = raw_connection(cast("FalkorDB", db))

    assert result is fake_connection


def test_graph_copy_handle_returns_selected_graph_unchanged() -> None:
    db = _FakeFalkorDB(_FakeConnection())

    result = graph_copy_handle(cast("FalkorDB", db), "some_graph")

    assert isinstance(result, _FakeGraph)


def test_fake_graph_query_handle_satisfies_graph_query_handle_protocol_shape() -> None:
    """Static-shape proof: fails `basedpyright`, not this assertion, if wrong."""
    graph: _GraphQueryHandle = _FakeGraphQueryHandle()

    graph.query("MATCH (n) RETURN n")


def test_graph_query_handle_returns_selected_graph_unchanged() -> None:
    db = _FakeFalkorDB(_FakeConnection())

    result = graph_query_handle(cast("FalkorDB", db), "some_graph")

    assert isinstance(result, _FakeGraph)
