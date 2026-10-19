"""Tests for the registered `check_instrument_ingestion_status` MCP tool.

The happy path: a fixed, parameterized
`UNWIND $celex_ids AS c MATCH (n:RegulatoryInstrument {celex: c}) RETURN
DISTINCT n.celex AS celex` query, run through `execute_cypher_query` via
`_resolve_graph` -- exactly like the `cypher` tool's own reliance on those
two helpers -- and the `_run_mcp_action` log triad, exactly like
`check_regulations`.

The degraded path: `_resolve_graph`/`execute_cypher_query`
failures (`McpGraphUnavailableError`, `GraphUnseededError`,
`QueryEngineExecutionError`, `WriteClauseRejectedError`) are now caught
inside the tool body and folded to `{"statuses": {c: "unknown" for c in
celex_ids}}`, never a top-level `error:` string -- so the call itself still
completes with `_run_mcp_action`'s `outcome="succeeded"`, deliberately
diverging from `check_regulations`'s own
`test_graph_unavailable_returns_named_error_when_single_tenant_graph_open_fails`
(`test_check_regulations_tool.py:286-320`), whose `error:`-prefixed return
trips `_run_mcp_action`'s `outcome="failed"` branch instead.

Authorization: this tool
intentionally declares no role gate (`AUTHENTICATED_ONLY`), matching `cypher`/
`near_misses_list` (both plain graph reads). The tests below prove that
placement from the tool-gate declarations (`TOOL_GATES`).

`check_regulations` declares a `ComplianceOfficer` `ROLE` gate (a sweep automatically
re-ingests detected amendments), so it is not used below as an *ungated*-peer comparison
for test (a) -- it sits in that same test's "gated" contrast alongside
`set-catalog-source`. Test (b) is unaffected: it is about registration mechanics, which
the gate does not change.

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_cypher_tool.py:47-99`'s own `_FakeQueryResult`/`_FakeGraphHandle`/
`_FakeFalkorDB` template (this file defines its own local copies rather than
importing them, matching every sibling MCP-interface test file's
convention). `pytest-asyncio` is not installed; this file drives the tool
with a bare `asyncio.run(server.call_tool(...))`, exactly like
`test_cypher_tool.py`/`test_check_regulations_tool.py`.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.tool_gates import TOOL_GATES, GateKind
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
    """`connect_from_config` raises a generic, detail-bearing
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


def test_audit_log_records_started_then_succeeded_and_logs_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """(b): the `mcp_interface` log shows a `started`->`succeeded` pair under one run id that
    carries no caller identity, as `check_regulations` does.
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
        assert "principal" not in line


# --- (c): pydantic-schema rejection smoke tests -------------------------------
# First list-typed MCP tool parameter in this codebase -- these
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


# --- degraded path -------------------------------


def test_graph_unavailable_folds_every_requested_celex_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a): a graph that cannot be opened at all -- `connect_from_config`
    raises, sanitised by `_resolve_graph` to `McpGraphUnavailableError` --
    never fails the whole call. Every requested `celex_ids` entry maps to
    `"unknown"`, and the result is `is_error is False` (a dict body, never
    a top-level `error:` string), so the artifact still returns the candidate list
    rather than the whole assessment failing.
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

    Deliberate choice: a fresh/unseeded graph is
    reported as `"unknown"`, NOT confidently `"not_yet_ingested"`, even
    though one could argue a totally empty graph knows for certain that
    nothing has been ingested. Reasoning: reusing `execute_cypher_query`
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
        assert "principal" not in line


# --- authorization parity proof ------------------


def test_tool_declares_no_role_gate_matching_plain_graph_read_peers() -> None:
    """(a): `check_instrument_ingestion_status` is declared `AUTHENTICATED_ONLY`, exactly like
    its plain-graph-read peers `cypher`/`near_misses_list`, and unlike the administrative or
    gated tools `set-catalog-source` and `check_regulations`, which declare a `ROLE` gate. This
    cohort placement -- not new gating code -- is what gives it the same authorization checks as
    other graph-read operations exposed via the MCP interface: no elevated `AccessRole` is ever
    checked for this tool, matching its peers exactly.
    """
    for peer in ("check_instrument_ingestion_status", "cypher", "near_misses_list"):
        gate = TOOL_GATES[peer]
        assert gate.kind is GateKind.AUTHENTICATED_ONLY, peer
        assert gate.role is None, peer

    # Contrast: administrative/gated tools declare a role gate on registration, proving the
    # peers above are ungated by comparison rather than trivially.
    assert TOOL_GATES["check_regulations"].kind is GateKind.ROLE
    assert TOOL_GATES["set-catalog-source"].kind is GateKind.ROLE


def test_tool_registered_on_server_the_same_ungated_way_as_check_regulations() -> None:
    """(b): `check_instrument_ingestion_status` is registered on the single
    `server` singleton the same way as `check_regulations`: both stay callable after decoration
    and both are present in the same `server` tool registry. This is purely about registration
    mechanics -- unaffected by `check_regulations`'s own role gate (see test (a) above); the
    "ungated" framing describes both tools' shared registration shape, not their authorization
    status.
    """
    assert callable(mcp_server.check_instrument_ingestion_status)
    assert callable(mcp_server.check_regulations)
    tool_names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert "check_instrument_ingestion_status" in tool_names
    assert "check_regulations" in tool_names
