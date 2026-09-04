"""Tests for `mcp_server.handle_mcp_tool_call` -- the injectable
`HandleMcpToolCall` core (PLAN_REVIEWED.md §6, Batch 2).

Covers AC-001, AC-002, AC-003, AC-004, AC-005 at the core (fakes only, no
server, no subprocess, no FalkorDB). The write-clause guard is proven to
live in Query Engine, never re-referenced here (AC-004, F-03: AST/symbol
checks, not substring scans).

Hand-written structural fakes throughout, per repo convention -- no
`unittest.mock`.
"""

from __future__ import annotations

import ast
import inspect
from typing import TYPE_CHECKING

import pytest

import ps_service.query_engine
from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.mcp_interface import mcp_server
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test pins the exact seed-check query text
    _WRITE_CLAUSE_REJECTION_MESSAGE,  # pyright: ignore[reportPrivateUsage]  # test pins the exact module-internal rejection wording
)
from ps_service.query_engine.models import QueryResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]
    type ReadLines = Callable[[Path], list[dict[str, object]]]


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted values."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally. `query()` records every call
    and either returns a scripted `_FakeQueryResult` or raises a scripted
    exception. Reports itself as seeded (D11) for `_SEED_CHECK_QUERY` --
    this module tests the pre-existing success/error/write-guard shapes,
    not the unseeded-graph guard (see `test_unseeded_error.py`), so a read
    query here must still reach the caller's own scripted result/error.
    """

    def __init__(
        self,
        *,
        result: _FakeQueryResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if self._error is not None:
            raise self._error
        if q == _SEED_CHECK_QUERY:
            return _FakeQueryResult(header=[[0, "c"]], result_set=[[1]])
        assert self._result is not None
        return self._result


def test_success_returns_columns_rows_row_count_dict(emitter: LogEmitter) -> None:
    fake = _FakeGraphHandle(
        result=_FakeQueryResult(
            header=[[0, "id"], [0, "name"]],
            result_set=[["a", "Alice"], ["b", "Bob"]],
        )
    )

    result = mcp_server.handle_mcp_tool_call(
        "MATCH (n) RETURN n.id, n.name", graph=fake, emitter=emitter
    )

    assert result == {
        "columns": ["id", "name"],
        "rows": [["a", "Alice"], ["b", "Bob"]],
        "row_count": 2,
    }


def test_delegates_to_execute_cypher_query(
    monkeypatch: pytest.MonkeyPatch, emitter: LogEmitter
) -> None:
    calls: list[dict[str, object]] = []

    def spy(
        query: str, *, graph: object, emitter: object = None, principal: object = None
    ) -> QueryResult:
        calls.append({"query": query, "graph": graph, "emitter": emitter, "principal": principal})
        return QueryResult(columns=["x"], rows=[[1]], row_count=1)

    monkeypatch.setattr(mcp_server, "execute_cypher_query", spy)
    fake = _FakeGraphHandle(result=_FakeQueryResult(header=[], result_set=[]))

    result = mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)

    assert len(calls) == 1
    assert calls[0]["graph"] is fake
    assert calls[0]["query"] == "MATCH (n) RETURN n"
    assert result == {"columns": ["x"], "rows": [[1]], "row_count": 1}


def test_principal_given_attaches_principal_to_query_engine_log_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Slice 4 (issue #67, AC-BI-008): `principal` passed into
    `handle_mcp_tool_call` reaches the `query_engine`/`execute_cypher_query`
    log entry end to end (not an `mcp_interface` entry -- this layer emits
    none of its own).
    """
    emitter, log_path = make_emitter()
    fake = _FakeGraphHandle(result=_FakeQueryResult(header=[], result_set=[]))

    mcp_server.handle_mcp_tool_call(
        "MATCH (n) RETURN n", graph=fake, emitter=emitter, principal="local-test-bypass"
    )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["component"] == "query_engine"
    assert entry["action"] == "execute_cypher_query"
    assert entry["principal"] == "local-test-bypass"


def test_no_principal_given_omits_principal_key_end_to_end(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Slice 4 (issue #67, AC-BI-008): omitting `principal=` (as every
    pre-existing caller does) leaves the key absent end to end, matching
    Slice 3's default behavior.
    """
    emitter, log_path = make_emitter()
    fake = _FakeGraphHandle(result=_FakeQueryResult(header=[], result_set=[]))

    mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert "principal" not in entry


def test_module_has_no_subprocess_or_sys() -> None:
    assert not hasattr(mcp_server, "subprocess")
    assert not hasattr(mcp_server, "sys")
    assert mcp_server.execute_cypher_query is ps_service.query_engine.execute_cypher_query


def test_error_string_returned_verbatim(emitter: LogEmitter) -> None:
    fake = _FakeGraphHandle(error=RuntimeError("boom"))

    result = mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)

    assert result == "error: boom"


def test_success_dict_not_rewrapped_or_truncated(emitter: LogEmitter) -> None:
    big_rows: list[object] = [[i, f"row-{i}"] for i in range(5000)]
    fake = _FakeGraphHandle(
        result=_FakeQueryResult(header=[[0, "n"], [0, "label"]], result_set=big_rows)
    )

    result = mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)

    assert isinstance(result, dict)
    assert result["row_count"] == 5000
    assert result["rows"] == [[i, f"row-{i}"] for i in range(5000)]
    assert result["columns"] == ["n", "label"]


def test_write_clause_rejected_and_graph_query_never_called(emitter: LogEmitter) -> None:
    fake = _FakeGraphHandle(result=_FakeQueryResult(header=[], result_set=[]))

    result = mcp_server.handle_mcp_tool_call("CREATE (n) RETURN n", graph=fake, emitter=emitter)

    assert result == f"error: {_WRITE_CLAUSE_REJECTION_MESSAGE}"
    assert fake.calls == []


def test_falkordb_execution_error_surfaced_verbatim(emitter: LogEmitter) -> None:
    fake = _FakeGraphHandle(error=RuntimeError("syntax error at offset 4"))

    result = mcp_server.handle_mcp_tool_call("MATCH (n RETURN n", graph=fake, emitter=emitter)

    assert result == "error: syntax error at offset 4"
    assert isinstance(result, str)
    assert "Traceback" not in result


def _module_ast() -> ast.Module:
    return ast.parse(inspect.getsource(mcp_server))


def _func_renders_as(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return (
            f"{_func_renders_as(func.value)}.{func.attr}"
            if isinstance(func.value, (ast.Name, ast.Attribute))
            else func.attr
        )
    return ""


def test_mcp_layer_does_not_reference_write_guard_symbols() -> None:
    tree = _module_ast()
    forbidden_names = {"re", "is_write_clause", "_WRITE_CLAUSE", "_WRITE_CLAUSE_REJECTION_MESSAGE"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name.split(".")[0] in forbidden_names for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden_names
            assert not any(alias.name in forbidden_names for alias in node.names)
        if isinstance(node, ast.Call):
            assert _func_renders_as(node.func) != "re.compile"
            assert not (isinstance(node.func, ast.Attribute) and node.func.attr == "query")

    assert not hasattr(mcp_server, "is_write_clause")
    assert not hasattr(mcp_server, "_WRITE_CLAUSE")
    assert not hasattr(mcp_server, "_WRITE_CLAUSE_REJECTION_MESSAGE")
