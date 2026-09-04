"""Tests for `handle_mcp_tool_call`'s `GraphUnseededError` delegation (D11, Slice 8.3).

PLAN.md Batch 8: `handle_mcp_tool_call` extends its existing
`except (WriteClauseRejectedError, QueryEngineExecutionError) as exc:` tuple
(`mcp_server.py:118`) with `GraphUnseededError`, returning `f"error: {exc}"`
-- the exact existing delegation shape, no new branch logic in MCP
Interface (L2 MCP Interface Patterns: "delegate, don't reimplement").

Both cases live in this one module, side by side, to prove AC-BI-014's
whole point: an unseeded graph and a seeded graph's own legitimate
zero-row answer are distinguishable signals, not the same thing observed
two different ways.

Hand-written structural fake throughout, per repo convention (no
`unittest.mock`) -- mirrors `test_handle_mcp_tool_call.py`'s and
`test_execute_cypher_query_unseeded.py`'s scripted-response-queue fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.mcp_interface import mcp_server
from ps_service.query_engine.cypher_query import (
    _GRAPH_UNSEEDED_DETAIL,  # pyright: ignore[reportPrivateUsage]  # test pins the exact sanitized detail string
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test pins the exact seed-check query text
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


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


class _ScriptedGraphHandle:
    """Satisfies `GraphHandle` structurally. Returns one scripted
    `_FakeQueryResult` per call, in order, and records every query text in
    `calls`.
    """

    def __init__(self, responses: list[_FakeQueryResult]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        return self._responses.pop(0)


def test_unseeded_graph_returns_sanitized_error_string(emitter: LogEmitter) -> None:
    fake = _ScriptedGraphHandle([_FakeQueryResult(header=[[0, "c"]], result_set=[[0]])])

    result = mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)

    assert result == f"error: {_GRAPH_UNSEEDED_DETAIL}"
    assert fake.calls == [_SEED_CHECK_QUERY]


def test_seeded_graph_genuinely_empty_result_still_returns_normal_shape(
    emitter: LogEmitter,
) -> None:
    """AC-BI-014's distinguishability proof: a seeded graph answering a
    specific query with zero rows is NOT the unseeded signal -- it still
    returns the normal `{columns, rows: [], row_count: 0}` envelope.
    """
    fake = _ScriptedGraphHandle(
        [
            _FakeQueryResult(header=[[0, "c"]], result_set=[[5]]),
            _FakeQueryResult(header=[[0, "n"]], result_set=[]),
        ]
    )

    result = mcp_server.handle_mcp_tool_call(
        "MATCH (n:Nonexistent) RETURN n", graph=fake, emitter=emitter
    )

    assert result == {"columns": ["n"], "rows": [], "row_count": 0}
    assert fake.calls == [_SEED_CHECK_QUERY, "MATCH (n:Nonexistent) RETURN n"]
