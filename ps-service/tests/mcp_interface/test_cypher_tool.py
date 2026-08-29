"""Tests for the registered `cypher` MCP tool driven through
`server.call_tool` (PLAN_REVIEWED.md §6, Batch 4).

Covers AC-001/002/003/005 via the tool boundary, the Q6 acquisition-failure
sanitisation split (host/port/driver/env-var detail must never cross the MCP
boundary -- L2 line 125), F-07 (`ServiceConfigurationError` and a FalkorDB
query rejection both stay `error:` return values, never a `ToolError`), and
F-16 (one `outcome="unavailable"` log entry on the connection-failure
branch).

`pytest-asyncio` is not installed; server coroutines are driven with bare
`asyncio.run(...)` (PLAN_REVIEWED.md Residual risk 8). Hand-written
structural fakes throughout -- no `unittest.mock`.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from mcp.types import CallToolResult, TextContent

from ps_service.config import ServiceConfigurationError
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.query_engine.cypher_query import (
    _WRITE_CLAUSE_REJECTION_MESSAGE,  # pyright: ignore[reportPrivateUsage]  # test pins the exact module-internal rejection wording
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter
    from ps_service.query_engine.falkordb_client import GraphHandle
    from ps_service.query_engine.models import QueryResult

    type ReadLines = Callable[[Path], list[dict[str, object]]]


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted values."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally. `query()` records every call
    and either returns a scripted `_FakeQueryResult` or raises a scripted
    exception.
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
        assert self._result is not None
        return self._result


class _FakeFalkorDB:
    """Stands in for the eager `falkordb.FalkorDB` client. `select_graph`
    records the requested name and returns the scripted handle.
    """

    def __init__(self, handle: _FakeGraphHandle) -> None:
        self._handle = handle
        self.selected: list[str] = []

    def select_graph(self, name: str) -> _FakeGraphHandle:
        self.selected.append(name)
        return self._handle


def _install_graph(monkeypatch: pytest.MonkeyPatch, handle: _FakeGraphHandle) -> _FakeFalkorDB:
    fake_db = _FakeFalkorDB(handle)

    def _connect_from_config(_config: object) -> _FakeFalkorDB:
        return fake_db

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect_from_config)
    return fake_db


def _call_cypher(query: str) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("cypher", {"query": query}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_success_via_call_tool_returns_json_content(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    handle = _FakeGraphHandle(
        result=_FakeQueryResult(
            header=[[0, "id"], [0, "name"]],
            result_set=[["a", "Alice"], ["b", "Bob"]],
        )
    )
    _install_graph(monkeypatch, handle)

    result = _call_cypher("MATCH (n) RETURN n.id, n.name")

    assert result.is_error is False
    assert json.loads(_text(result)) == {
        "columns": ["id", "name"],
        "rows": [["a", "Alice"], ["b", "Bob"]],
        "row_count": 2,
    }


def test_delegates_in_process_via_call_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    real_execute = mcp_server.execute_cypher_query
    seen: list[str] = []

    def spy(query: str, *, graph: GraphHandle, emitter: LogEmitter | None = None) -> QueryResult:
        seen.append(query)
        return real_execute(query, graph=graph, emitter=emitter)

    monkeypatch.setattr(mcp_server, "execute_cypher_query", spy)
    handle = _FakeGraphHandle(result=_FakeQueryResult(header=[], result_set=[]))
    _install_graph(monkeypatch, handle)

    result = _call_cypher("MATCH (n) RETURN n")

    assert seen == ["MATCH (n) RETURN n"]
    assert result.is_error is False
    assert not hasattr(mcp_server, "subprocess")


def test_write_clause_error_propagated_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()
    handle = _FakeGraphHandle(result=_FakeQueryResult(header=[], result_set=[]))
    _install_graph(monkeypatch, handle)

    result = _call_cypher("CREATE (n) RETURN n")

    assert result.is_error is False
    assert _text(result) == f"error: {_WRITE_CLAUSE_REJECTION_MESSAGE}"
    assert handle.calls == []


def test_falkordb_rejection_via_call_tool_returns_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure()
    handle = _FakeGraphHandle(error=RuntimeError("syntax error at offset 4"))
    _install_graph(monkeypatch, handle)

    # Must NOT raise ToolError -- a FalkorDB rejection is a result-shaped error.
    result = _call_cypher("MATCH (n RETURN n")

    assert result.is_error is False
    assert _text(result) == "error: syntax error at offset 4"
    assert "Traceback" not in _text(result)


def test_connection_failure_returns_sanitised_error(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()

    def boom(_config: object) -> object:
        raise redis.exceptions.ConnectionError(
            "Error 61 connecting to db-host:6379. Connection refused."
        )

    monkeypatch.setattr(mcp_server, "connect_from_config", boom)

    result = _call_cypher("MATCH (n) RETURN n")

    assert result.is_error is False
    text = _text(result)
    assert text == "error: the policy graph database is not reachable"
    for leak in ("db-host", "6379", "Connection refused", "Traceback"):
        assert leak not in text


def test_service_configuration_error_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    configure()

    def boom() -> object:
        raise ServiceConfigurationError("PS_FALKORDB_PORT must be an integer, got 'abc'")

    monkeypatch.setattr(mcp_server, "load_config", boom)

    result = _call_cypher("MATCH (n) RETURN n")

    assert result.is_error is False
    text = _text(result)
    assert text == "error: the policy graph database is not reachable"
    assert "PS_FALKORDB_PORT" not in text
    assert "abc" not in text


def test_connection_failure_emits_unavailable_log_entry(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()

    def boom(_config: object) -> object:
        raise redis.exceptions.ConnectionError(
            "Error 61 connecting to db-host:6379. Connection refused."
        )

    monkeypatch.setattr(mcp_server, "connect_from_config", boom)

    _call_cypher("MATCH (secret {ssn: '123-45-6789'}) RETURN secret")
    emitter.flush()

    log_path = resolve_default_log_path()
    lines = read_lines(log_path)
    unavailable = [entry for entry in lines if entry.get("outcome") == "unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0]["component"] == "mcp_interface"
    assert unavailable[0]["action"] == "handle_mcp_tool_call"

    raw = log_path.read_text(encoding="utf-8")
    assert "123-45-6789" not in raw
    assert "db-host" not in raw
    assert "6379" not in raw


def test_resolve_graph_wraps_any_failure_as_graph_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_config: object) -> object:
        raise RuntimeError("boom 10.0.0.5:6379")

    monkeypatch.setattr(mcp_server, "connect_from_config", boom)

    with pytest.raises(McpGraphUnavailableError) as excinfo:
        mcp_server._resolve_graph()  # pyright: ignore[reportPrivateUsage]  # test drives the module-internal graph resolver directly

    message = str(excinfo.value)
    assert message == mcp_server._GRAPH_UNAVAILABLE_DETAIL  # pyright: ignore[reportPrivateUsage]  # test pins the module-internal sanitised detail string
    assert "10.0.0.5" not in message
    assert "6379" not in message
