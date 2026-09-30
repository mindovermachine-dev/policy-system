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

from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.errors import McpGraphUnavailableError
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test pins the exact seed-check query text
    _WRITE_CLAUSE_REJECTION_MESSAGE,  # pyright: ignore[reportPrivateUsage]  # test pins the exact module-internal rejection wording
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    type ReadLines = Callable[[Path], list[dict[str, object]]]


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted values."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally. `query()` records every call
    and either returns a scripted `_FakeQueryResult` or raises a scripted
    exception. Reports itself as seeded (D11) for `_SEED_CHECK_QUERY` --
    this module tests the pre-existing tool-boundary shapes, not the
    unseeded-graph guard, so a read query here must still reach the
    caller's own scripted result/error.
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
        self.timeouts: list[int | None] = []

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        self.calls.append(q)
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        if q == _SEED_CHECK_QUERY:
            return _FakeQueryResult(header=[[0, "c"]], result_set=[[1]])
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


def test_cypher_tool_passes_config_query_timeout_ms_and_row_cap_to_execute_cypher_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2 (pulled forward from S4 per CHANGES.md Flaw 1): `cypher()` resolves
    `config = load_config()` and must thread `config.query_timeout_ms`/
    `config.query_row_cap` all the way into the real, unmonkeypatched
    `execute_cypher_query` -- the one real production call site, not just a
    test-only signature. Proven end to end via state/output instead of a
    spy: `timeout_ms` is recorded by the boundary fake's own `graph.query`
    call (the real collaboration surface), and `row_cap`'s effect is proven
    by the returned `row_count`/`truncated` shape, mirroring
    `test_truncated_result_propagates_true_through_cypher_tool` immediately
    below.
    """
    monkeypatch.setenv("PS_QUERY_ROW_CAP", "2")
    configure()
    handle = _FakeGraphHandle(
        result=_FakeQueryResult(
            header=[[0, "id"]],
            result_set=[["a"], ["b"], ["c"], ["d"]],
        )
    )
    _install_graph(monkeypatch, handle)
    config = mcp_server.load_config()

    result = _call_cypher("MATCH (n) RETURN n.id")

    assert result.is_error is False
    assert handle.calls[-1] == "MATCH (n) RETURN n.id"
    assert handle.timeouts[-1] == config.query_timeout_ms
    body = json.loads(_text(result))
    assert body["row_count"] == config.query_row_cap
    assert body["truncated"] is True


def test_truncated_result_propagates_true_through_cypher_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S4 (issue #38, AC-BI-008): a result set exceeding the configured
    `PS_QUERY_ROW_CAP` reaches the real `cypher()` tool's returned dict with
    `truncated: True` -- proof that `QueryResult.truncated` (S3) actually
    crosses the MCP boundary through the real production call path, not
    just through a direct `handle_mcp_tool_call(...)` call.
    """
    monkeypatch.setenv("PS_QUERY_ROW_CAP", "2")
    configure()
    handle = _FakeGraphHandle(
        result=_FakeQueryResult(
            header=[[0, "id"]],
            result_set=[["a"], ["b"], ["c"], ["d"]],
        )
    )
    _install_graph(monkeypatch, handle)

    result = _call_cypher("MATCH (n) RETURN n.id")

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["truncated"] is True
    assert body["row_count"] == 2


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
        "truncated": False,
    }


def test_delegates_in_process_via_call_tool(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """S2: `cypher()` reaches the real, in-process, unmonkeypatched
    `execute_cypher_query` (never a subprocess, never a reimplementation) --
    proven by the real `query_engine`/`execute_cypher_query` log entry only
    that function emits, plus the fake `graph` collaborator's own recorded
    call, mirroring `test_cypher_tool_attaches_local_test_principal_when_bypass_active`'s
    already-established real-execution-plus-state-assertion pattern, rather
    than spying on the delegate call.
    """
    emitter = configure()
    handle = _FakeGraphHandle(result=_FakeQueryResult(header=[], result_set=[]))
    _install_graph(monkeypatch, handle)

    result = _call_cypher("MATCH (n) RETURN n")

    assert result.is_error is False
    assert handle.calls[-1] == "MATCH (n) RETURN n"
    assert not hasattr(mcp_server, "subprocess")
    emitter.flush()
    lines = read_lines(resolve_default_log_path())
    entry = next(
        line
        for line in lines
        if line.get("component") == "query_engine" and line.get("action") == "execute_cypher_query"
    )
    assert entry["outcome"] == "succeeded"


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
    """A real malformed `PS_FALKORDB_PORT` drives the real `load_config()`
    into its own `ServiceConfigurationError` path (sibling tests in this
    file, e.g. `PS_QUERY_ROW_CAP` above, already use the same real-env-var
    shape) -- not a monkeypatched `load_config` standing in for it.
    """
    configure()
    monkeypatch.setenv("PS_FALKORDB_PORT", "abc")

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
        mcp_server._resolve_graph(  # pyright: ignore[reportPrivateUsage]  # test drives the module-internal graph resolver directly
            mcp_server.load_config()
        )

    message = str(excinfo.value)
    assert message == mcp_server._GRAPH_UNAVAILABLE_DETAIL  # pyright: ignore[reportPrivateUsage]  # test pins the module-internal sanitised detail string
    assert "10.0.0.5" not in message
    assert "6379" not in message


def test_cypher_tool_attaches_local_test_principal_when_bypass_active(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """Slice 5 (issue #67, AC-BI-005, AC-BI-008): with the bypass active, a
    query answered successfully through the real `cypher()` tool carries the
    fixed local principal on its `query_engine` log entry -- the concrete
    proof, at this boundary, that the bypass answers queries with no
    credential ever requested or checked anywhere in the call path.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    handle = _FakeGraphHandle(result=_FakeQueryResult(header=[[0, "id"]], result_set=[["a"]]))
    _install_graph(monkeypatch, handle)

    result = _call_cypher("MATCH (n) RETURN n.id")

    assert result.is_error is False
    emitter.flush()
    lines = read_lines(resolve_default_log_path())
    entry = next(
        line
        for line in lines
        if line.get("component") == "query_engine" and line.get("action") == "execute_cypher_query"
    )
    assert entry["principal"] == mcp_server.LOCAL_TEST_PRINCIPAL_ID


def test_cypher_tool_omits_principal_when_bypass_inactive(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """Slice 5 (issue #67, AC-BI-008): with the bypass inactive (the
    default), the `query_engine` log entry carries no `principal` key at
    all -- the real `cypher()` tool never invents an identity when the
    bypass is off.
    """
    emitter = configure()
    handle = _FakeGraphHandle(result=_FakeQueryResult(header=[[0, "id"]], result_set=[["a"]]))
    _install_graph(monkeypatch, handle)

    result = _call_cypher("MATCH (n) RETURN n.id")

    assert result.is_error is False
    emitter.flush()
    lines = read_lines(resolve_default_log_path())
    entry = next(
        line
        for line in lines
        if line.get("component") == "query_engine" and line.get("action") == "execute_cypher_query"
    )
    assert "principal" not in entry
