"""Tests for the registered `check_instrument_ingestion_status` MCP tool
(issue #139, S4/S5/S6).

S4 covers the happy path only (PLAN.md §3, S4): a fixed, parameterized
`UNWIND $celex_ids AS c MATCH (n:RegulatoryInstrument {celex: c}) RETURN
DISTINCT n.celex AS celex` query, run through `execute_cypher_query` via
`_resolve_graph` -- exactly like the `cypher` tool's own reliance on those
two helpers -- and the `_resolve_principal` + `_run_mcp_action` audit triad,
exactly like `check_regulations`.

S5 (PLAN.md D5) adds the degraded path: `_resolve_graph`/`execute_cypher_query`
failures (`McpGraphUnavailableError`, `GraphUnseededError`,
`QueryEngineExecutionError`, `WriteClauseRejectedError`) are now caught
inside the tool body and folded to `{"statuses": {c: "unknown" for c in
celex_ids}}`, never a top-level `error:` string -- so the call itself still
completes with `_run_mcp_action`'s `outcome="succeeded"`, deliberately
diverging from `check_regulations`'s own
`test_graph_unavailable_returns_named_error_when_single_tenant_graph_open_fails`
(`test_check_regulations_tool.py:286-320`), whose `error:`-prefixed return
trips `_run_mcp_action`'s `outcome="failed"` branch instead.

S6 (PLAN.md D6, AC-BI-010) adds no new production authz code -- this tool
intentionally carries no `require_role` gate, matching `cypher`/
`check_regulations`/`near_misses_list` (all plain graph reads). The tests
below prove that placement structurally (AST, mirroring
`test_scope_guard.py`'s own convention) and prove the resolved principal
under the local-test bypass matches `check_regulations`'s own audit-log
parity (`test_check_regulations_tool.py:91-101, 244-245`).

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_cypher_tool.py:47-99`'s own `_FakeQueryResult`/`_FakeGraphHandle`/
`_FakeFalkorDB` template (this file defines its own local copies rather than
importing them, matching every sibling MCP-interface test file's
convention). `pytest-asyncio` is not installed; this file drives the tool
with a bare `asyncio.run(server.call_tool(...))`, exactly like
`test_cypher_tool.py`/`test_check_regulations_tool.py`.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from typing import TYPE_CHECKING

import pytest
from api._fakes import build_fake_change_check_dependencies
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test must answer the pre-flight seed-check query too, mirrors test_cypher_tool.py's own convention
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
    (query text and bound `params`) and answers `_SEED_CHECK_QUERY` as
    seeded by default (this file tests the ingestion-status query shape,
    not the unseeded-graph guard, unless `unseeded=True` is given -- S5's
    `GraphUnseededError` test), returning a scripted `_FakeQueryResult` for
    the tool's own query.
    """

    def __init__(self, *, ingested_celex: tuple[str, ...], unseeded: bool = False) -> None:
        self._ingested_celex = ingested_celex
        self._unseeded = unseeded
        self.calls: list[str] = []
        self.params_seen: list[dict[str, object] | None] = []

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        self.calls.append(q)
        self.params_seen.append(params)
        if q == _SEED_CHECK_QUERY:
            count = 0 if self._unseeded else 1
            return _FakeQueryResult(header=[[0, "c"]], result_set=[[count]])
        return _FakeQueryResult(
            header=[[0, "celex"]],
            result_set=[[celex] for celex in self._ingested_celex],
        )


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


def _install_unreachable_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """S5 (a): `connect_from_config` raises a generic, detail-bearing
    exception -- `_resolve_graph` catches it broadly and sanitises it to
    `McpGraphUnavailableError` (mcp_server.py's own `_resolve_graph`
    docstring), exactly like `test_check_regulations_tool.py`'s
    `test_graph_unavailable_returns_named_error_when_single_tenant_graph_open_fails`
    does for `open_single_tenant`.
    """

    def _raising_connect_from_config(_config: object) -> object:
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    monkeypatch.setattr(mcp_server, "connect_from_config", _raising_connect_from_config)


def _call_check_instrument_ingestion_status(celex_ids: list[str]) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool("check_instrument_ingestion_status", {"celex_ids": celex_ids})
    )
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_statuses_split_ingested_from_not_yet_ingested(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a): a fake graph returning rows for some requested CELEX ids and not
    others -- `statuses` correctly splits `ingested`/`not_yet_ingested`, and
    the query is parameterized (the raw `celex_ids` list is bound as
    `params["celex_ids"]`, never string-interpolated into the query text).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    handle = _FakeGraphHandle(ingested_celex=("32014R0910", "32016R0679"))
    _install_graph(monkeypatch, handle)
    celex_ids = ["32014R0910", "32023R1114", "32016R0679"]

    result = _call_check_instrument_ingestion_status(celex_ids)

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {
        "statuses": {
            "32014R0910": "ingested",
            "32023R1114": "not_yet_ingested",
            "32016R0679": "ingested",
        }
    }
    real_calls = [
        (q, p)
        for q, p in zip(handle.calls, handle.params_seen, strict=True)
        if q != _SEED_CHECK_QUERY
    ]
    assert len(real_calls) == 1
    query_text, params = real_calls[0]
    assert "UNWIND $celex_ids" in query_text
    assert params == {"celex_ids": celex_ids}
    for celex in celex_ids:
        assert celex not in query_text


def test_audit_log_records_started_then_succeeded_with_resolved_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """(b): the `mcp_interface` audit log shows a `started`->`succeeded`
    pair carrying the resolved principal, mirroring
    `test_check_regulations_tool.py:234-245`.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    handle = _FakeGraphHandle(ingested_celex=("32014R0910",))
    _install_graph(monkeypatch, handle)

    result = _call_check_instrument_ingestion_status(["32014R0910"])

    assert result.is_error is False
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface"
        and line.get("action") == "check_instrument_ingestion_status"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line.get("run_id") for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


# --- (c): pydantic-schema rejection smoke tests -------------------------------
# First list-typed MCP tool parameter in this codebase (PLAN.md §1) -- these
# prove the `Annotated[list[Annotated[str, Field(pattern=...)]], Field(min_length=...,
# max_length=...)]` composition actually registers and validates correctly.


def test_empty_celex_ids_list_is_rejected_at_the_schema_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty `celex_ids` list violates the outer `Field(min_length=1)`
    constraint -- rejected before the tool body ever runs (mirrors
    `test_restore_instrument_tool.py`'s own schema-rejection convention: a
    bare in-process `server.call_tool` propagates a schema rejection as a
    raised `ToolError`, not a returned `CallToolResult(is_error=True)`).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    handle = _FakeGraphHandle(ingested_celex=())
    _install_graph(monkeypatch, handle)

    with pytest.raises(ToolError):
        _call_check_instrument_ingestion_status([])

    assert handle.calls == []


def test_malformed_celex_item_is_rejected_at_the_schema_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `celex_ids` element that doesn't match `_CELEX_PATTERN` (wrong
    length/shape, not a real CELEX) violates the inner element `Field(pattern=...)`
    constraint -- rejected the same way an empty list is, before the tool
    body ever runs.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    handle = _FakeGraphHandle(ingested_celex=())
    _install_graph(monkeypatch, handle)

    with pytest.raises(ToolError):
        _call_check_instrument_ingestion_status(["not-a-celex"])

    assert handle.calls == []


# --- S5: degraded path (PLAN.md D5, AC-BI-009) -------------------------------


def test_graph_unavailable_folds_every_requested_celex_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a): a graph that cannot be opened at all -- `connect_from_config`
    raises, sanitised by `_resolve_graph` to `McpGraphUnavailableError` --
    never fails the whole call. Every requested `celex_ids` entry maps to
    `"unknown"`, and the result is `is_error is False` (a dict body, never
    a top-level `error:` string), per AC-BI-009's literal wording ("the
    artifact still returns the candidate list ... rather than the whole
    assessment failing").
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _install_unreachable_graph(monkeypatch)
    celex_ids = ["32014R0910", "32023R1114", "32016R0679"]

    result = _call_check_instrument_ingestion_status(celex_ids)

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {"statuses": dict.fromkeys(celex_ids, "unknown")}


def test_unseeded_graph_folds_every_requested_celex_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b): a totally unseeded graph (`_SEED_CHECK_QUERY`'s `count(n)`
    answers 0) raises `GraphUnseededError` inside `execute_cypher_query`,
    caught by the same fold as any other graph-availability problem --
    every requested `celex_ids` entry maps to `"unknown"`, `is_error is
    False`, never a top-level `error:` string.

    Deliberate choice (PLAN.md D5, traceable to this plan, independently
    confirmed CORRECT by the Critique stage): a fresh/unseeded graph is
    reported as `"unknown"`, NOT confidently `"not_yet_ingested"`, even
    though one could argue a totally empty graph knows for certain that
    nothing has been ingested. D5's reasoning: reusing `execute_cypher_query`
    whole (rather than bypassing its seeded-check) avoids duplicating the
    write-guard, and an unseeded graph is at least as likely to signal a
    real provisioning/connectivity problem as a genuinely fresh company
    graph -- so it gets the same cautious `"unknown"` answer as any other
    graph-availability failure, not a confident negative.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    handle = _FakeGraphHandle(ingested_celex=(), unseeded=True)
    _install_graph(monkeypatch, handle)
    celex_ids = ["32014R0910", "32023R1114"]

    result = _call_check_instrument_ingestion_status(celex_ids)

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body == {"statuses": dict.fromkeys(celex_ids, "unknown")}
    # The tool's own query must never have been reached once the seed check
    # itself reports zero content.
    assert handle.calls == [_SEED_CHECK_QUERY]


def test_degraded_path_audit_log_shows_succeeded_not_failed(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """(c): the `mcp_interface` audit log shows `outcome="succeeded"` (not
    `"failed"`) for a degraded-path call. This is the call-level success
    semantics that deliberately diverges from `check_regulations`'s own
    `test_graph_unavailable_returns_named_error_when_single_tenant_graph_open_fails`
    (`test_check_regulations_tool.py:286-320`): that sibling test's tool
    body returns an `error:`-prefixed *string*, which trips
    `_run_mcp_action`'s `outcome="failed"` branch (mcp_server.py's
    `_run_mcp_action` docstring). Here the degraded-path fold returns a
    *dict* (`{"statuses": {...: "unknown"}}`), so `_run_mcp_action` logs
    the call as an ordinary success.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    _install_unreachable_graph(monkeypatch)

    result = _call_check_instrument_ingestion_status(["32014R0910"])

    assert result.is_error is False
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface"
        and line.get("action") == "check_instrument_ingestion_status"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


# --- S6: authorization parity proof (PLAN.md D6, AC-BI-010) ------------------


def _calls_in(func: object) -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(func))  # type: ignore[arg-type]
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _call_func_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_tool_body_has_no_require_role_gate_matching_plain_graph_read_peers() -> None:
    """(a): proves, structurally via AST -- not a substring scan, mirroring
    `test_scope_guard.py`'s own convention (F-03: a bare scan false-fails
    on docstring text) -- that `check_instrument_ingestion_status`'s own
    function body contains no call to `require_role`, exactly like its
    plain-graph-read peers `cypher`/`check_regulations`/`near_misses_list`,
    and unlike an administrative tool such as `set_catalog_source`, which
    does call `require_role`. Per PLAN.md D6, this cohort placement -- not
    new gating code -- is what satisfies AC-BI-010's "same authorization
    checks as other graph-read operations exposed via the MCP interface":
    no elevated `AccessRole` is ever checked for this tool, matching its
    peers exactly.
    """
    ungated_peers = (
        mcp_server.check_instrument_ingestion_status,
        mcp_server.cypher,
        mcp_server.check_regulations,
        mcp_server.near_misses_list,
    )
    for peer in ungated_peers:
        names = {_call_func_name(c) for c in _calls_in(peer)}
        assert "require_role" not in names, peer

    # Contrast: an administrative tool in the same module DOES gate via
    # `require_role`, proving this AST technique actually discriminates
    # rather than trivially passing for everything.
    gated_names = {_call_func_name(c) for c in _calls_in(mcp_server.set_catalog_source)}
    assert "require_role" in gated_names


def test_tool_registered_on_server_the_same_ungated_way_as_check_regulations() -> None:
    """(b): `check_instrument_ingestion_status` is registered on the single
    `server` singleton the same un-gated way as `check_regulations` -- a
    bare `@server.tool()` with no `name=` kebab-case override (PLAN.md §1:
    the explicit `name=` override is reserved for admin/write tools like
    `set-catalog-source`/`invite-user`/`grant-access-role`) and no
    additional wrapping: both remain plain functions after decoration
    (`server.tool()` returns the original callable), and both are present
    in the same `server` tool registry.
    """
    assert callable(mcp_server.check_instrument_ingestion_status)
    assert callable(mcp_server.check_regulations)
    tool_names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert "check_instrument_ingestion_status" in tool_names
    assert "check_regulations" in tool_names


def test_local_test_bypass_principal_matches_check_regulations_parity(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """(c): under the local-test bypass, `LOCAL_TEST_PRINCIPAL_ID` appears
    on this tool's audit-log row exactly as it does for `check_regulations`
    (`test_check_regulations_tool.py:91-101, 244-245`) -- both tools
    resolve their caller identity through the same `_resolve_principal`
    path, proving no additional identity/role resolution branch was
    silently added for this tool.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    handle = _FakeGraphHandle(ingested_celex=("32014R0910",))
    _install_graph(monkeypatch, handle)
    fake = build_fake_change_check_dependencies()
    monkeypatch.setattr(
        mcp_server, "build_default_change_check_dependencies", lambda: fake.dependencies
    )

    ingestion_result = _call_check_instrument_ingestion_status(["32014R0910"])
    regulations_result = asyncio.run(mcp_server.server.call_tool("check_regulations", {}))

    assert ingestion_result.is_error is False
    assert isinstance(regulations_result, CallToolResult)
    assert regulations_result.is_error is False

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    ingestion_principals = {
        line.get("principal")
        for line in all_lines
        if line.get("component") == "mcp_interface"
        and line.get("action") == "check_instrument_ingestion_status"
    }
    regulations_principals = {
        line.get("principal")
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "check_regulations"
    }
    assert ingestion_principals == regulations_principals == {LOCAL_TEST_PRINCIPAL_ID}
