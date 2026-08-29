"""Tests for `execute_cypher_query`'s success-path shape mapping (AC-001).

PLAN_REVIEWED.md §5, Increment 4: a scripted fake `GraphHandle`/
`GraphQueryResult` proves the exact `QueryResult(columns, rows, row_count)`
mapping from FalkorDB's `header`/`result_set` shape, plus the empty-result
and falsy-header edge cases.

Batch 4/Increment 6 wired `_log` into `execute_cypher_query`'s success path,
so every call here now needs a live emitter (or a configured process
default) or it raises `LoggingLifecycleError` -- mirrors
`tests/llm_interface/test_route_completion_mocked.py`'s established
throwaway-emitter fixture pattern; these tests don't assert on log content,
only that `execute_cypher_query` itself behaves correctly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.query_engine.cypher_query import execute_cypher_query
from ps_service.query_engine.models import QueryResult

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


class _ScriptedQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted `header`/
    `result_set` values.
    """

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _ScriptedGraphHandle:
    """Satisfies `GraphHandle` structurally -- always returns the one
    scripted `_ScriptedQueryResult` regardless of the query text.
    """

    def __init__(self, result: _ScriptedQueryResult) -> None:
        self._result = result

    def query(self, q: str, params: dict[str, object] | None = None) -> _ScriptedQueryResult:
        return self._result


def test_success_path_maps_header_and_result_set_to_exact_query_result(emitter: LogEmitter) -> None:
    scripted = _ScriptedQueryResult(
        header=[[0, "id"], [0, "name"]],
        result_set=[["a", "Alice"], ["b", "Bob"]],
    )
    fake_graph = _ScriptedGraphHandle(scripted)

    result = execute_cypher_query(
        "MATCH (n) RETURN n.id, n.name", graph=fake_graph, emitter=emitter
    )

    assert result == QueryResult(
        columns=["id", "name"],
        rows=[["a", "Alice"], ["b", "Bob"]],
        row_count=2,
    )


def test_empty_result_set_yields_zero_row_count_and_empty_rows(emitter: LogEmitter) -> None:
    scripted = _ScriptedQueryResult(header=[[0, "id"]], result_set=[])
    fake_graph = _ScriptedGraphHandle(scripted)

    result = execute_cypher_query("MATCH (n) RETURN n.id", graph=fake_graph, emitter=emitter)

    assert result.rows == []
    assert result.row_count == 0
    assert result.columns == ["id"]


def test_falsy_header_yields_empty_columns_list(emitter: LogEmitter) -> None:
    scripted = _ScriptedQueryResult(header=[], result_set=[])
    fake_graph = _ScriptedGraphHandle(scripted)

    result = execute_cypher_query("MATCH (n) RETURN n", graph=fake_graph, emitter=emitter)

    assert result.columns == []
    assert result.rows == []
    assert result.row_count == 0
