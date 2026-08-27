"""Batch 8 -- the `falkordb_live` end-to-end capstone for the MCP Interface
query path (PLAN_REVIEWED.md §6 Batch 8), proving AC-001 / AC-004 / AC-005
against real infrastructure.

Mirrors `tests/query_engine/test_live_capstone.py`'s shape exactly (marker,
fixture style, direct-read `_count_nodes` helper). Connects to the real,
reachable FalkorDB instance and selects the real, already-populated
`policy_system` graph (~776 nodes) -- never a disposable test graph. The
whole file is read-only by construction: every query either goes through
`handle_mcp_tool_call` -> `execute_cypher_query`'s write-clause guard (which
`test_live_write_clause_rejected` below proves rejects before FalkorDB ever
sees the write), or is a plain `MATCH ... RETURN count(n)` read issued
directly against the real `GraphHandle`.

Three tests, all `@pytest.mark.falkordb_live` (deselected by the default
`-m "not falkordb_live and not llm_live"` expression):

1. `test_live_read_only_query_returns_expected_shape` -- a real
   `MATCH (n) RETURN n LIMIT 3` via `handle_mcp_tool_call` returns a `dict`
   whose `row_count` equals `len(rows)`, is `<= 3`, and whose `columns` is
   exactly `["n"]` (AC-001 end-to-end).
2. `test_live_malformed_query_returns_verbatim_error` -- a syntactically
   invalid query returns a `str` starting `"error: "` carrying FalkorDB's
   own error text, with no `"Traceback"` (AC-005 end-to-end).
3. `test_live_write_clause_rejected` -- a `CREATE ...` query returns exactly
   `"error: " + _WRITE_CLAUSE_REJECTION_MESSAGE`, and a direct
   `MATCH (n) RETURN count(n)` taken before vs. after proves the graph is
   provably unmutated -- the guard rejected before FalkorDB was touched
   (AC-004 end-to-end).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.mcp_interface.mcp_server import handle_mcp_tool_call
from ps_service.query_engine.cypher_query import _WRITE_CLAUSE_REJECTION_MESSAGE
from ps_service.query_engine.falkordb_client import GraphHandle, connect, select_graph

_HOST = "127.0.0.1"
_PORT = 6379
_GRAPH_NAME = "policy_system"


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    """Throwaway `LogEmitter` -- `handle_mcp_tool_call` delegates to
    `execute_cypher_query`, which logs on every branch (succeeded / rejected
    / failed), so it needs a live emitter rather than relying on a
    configured process default. No test here asserts on log content.
    Mirrors `tests/query_engine/test_execute_cypher_query_success.py`."""
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


@pytest.fixture
def real_graph() -> GraphHandle:
    """Connects to the real, reachable FalkorDB instance and selects the
    real `policy_system` graph via the real `connect`/`select_graph` from
    `falkordb_client.py` -- no fake `GraphHandle`. Same host/port/graph as
    `tests/query_engine/test_live_capstone.py`."""
    db = connect(host=_HOST, port=_PORT)
    return select_graph(db, _GRAPH_NAME)


def _count_nodes(graph: GraphHandle) -> int:
    """A legitimate direct read against the real graph, bypassing
    `handle_mcp_tool_call` -- used only to observe the real node count
    before/after the guarded write attempt below. Routing this through the
    function under test would be circular; this is a plain
    `MATCH ... RETURN count(n)` query, never anything else."""
    result = graph.query("MATCH (n) RETURN count(n)")
    rows = cast("list[list[object]]", result.result_set)
    return cast(int, rows[0][0])


@pytest.mark.falkordb_live
def test_live_read_only_query_returns_expected_shape(
    real_graph: GraphHandle, emitter: LogEmitter
) -> None:
    """AC-001 live proof: a real `MATCH (n) RETURN n LIMIT 3` query against
    the real `policy_system` graph, executed through the actual
    `handle_mcp_tool_call` -> `execute_cypher_query` -> real FalkorDB driver.
    """
    result = handle_mcp_tool_call("MATCH (n) RETURN n LIMIT 3", graph=real_graph, emitter=emitter)

    assert isinstance(result, dict)
    row_count = cast(int, result["row_count"])
    rows = cast("list[object]", result["rows"])
    assert row_count == len(rows)
    assert row_count <= 3
    assert result["columns"] == ["n"], (
        "columns must match the RETURN clause's own alias exactly -- real driver response shape"
    )


@pytest.mark.falkordb_live
def test_live_malformed_query_returns_verbatim_error(
    real_graph: GraphHandle, emitter: LogEmitter
) -> None:
    """AC-005 live proof: a syntactically invalid query is executed against
    the real FalkorDB, which rejects it; `handle_mcp_tool_call` surfaces
    FalkorDB's own error text verbatim as an `error: ` string, never a
    formatted traceback."""
    result = handle_mcp_tool_call("MATCH (n RETURN n", graph=real_graph, emitter=emitter)

    assert isinstance(result, str)
    assert result.startswith("error: ")
    assert "Invalid input" in result, (
        f"expected FalkorDB's own parser error text, got: {result!r}"
    )
    assert "Traceback" not in result


@pytest.mark.falkordb_live
def test_live_write_clause_rejected(real_graph: GraphHandle, emitter: LogEmitter) -> None:
    """AC-004 live proof: a write-clause query against the real graph is
    rejected by Query Engine's guard *before* FalkorDB is ever touched.
    Proven two ways: the returned string is exactly
    `"error: " + _WRITE_CLAUSE_REJECTION_MESSAGE`, and the real graph's node
    count is provably unchanged before vs. after (the `CapstoneProbe` node
    never persisted)."""
    count_before = _count_nodes(real_graph)

    result = handle_mcp_tool_call(
        "CREATE (x:CapstoneProbe) RETURN x", graph=real_graph, emitter=emitter
    )

    assert result == f"error: {_WRITE_CLAUSE_REJECTION_MESSAGE}"

    count_after = _count_nodes(real_graph)
    assert count_after == count_before, (
        f"policy_system's node count changed ({count_before} -> {count_after}) -- "
        "the write-clause guard must reject before graph.query is ever called"
    )

    probe_rows = cast(
        "list[list[object]]",
        real_graph.query("MATCH (x:CapstoneProbe) RETURN count(x)").result_set,
    )
    assert probe_rows[0][0] == 0, "the rejected CREATE must not have persisted a CapstoneProbe node"
