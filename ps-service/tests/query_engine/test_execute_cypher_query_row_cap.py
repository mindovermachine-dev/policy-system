"""Tests for `execute_cypher_query`'s row-count cap enforcement (AC-BI-003, AC-BI-006, AC-BI-007).

PLAN.md §4 S3 / D1: the row cap is applied strictly *after* `graph.query()`
returns -- a pure Python-side `rows[:row_cap]` slice on `execute_cypher_query`'s
own already-mapped `rows` list. Never sent to FalkorDB as a query modifier and
never touching `GRAPH.CONFIG SET RESULTSET_SIZE` (AC-BI-007) -- the cap only
ever fires on the success path, after FalkorDB has already computed and
returned every row.

`row_count` (AC-BI-006) reflects the *post-slice* (returned) count, not the
pre-truncation count -- `QueryResult.truncated` is the only signal a caller
gets that more rows existed.

Hand-written structural fakes throughout, per repo convention (no
`unittest.mock`), mirroring `test_execute_cypher_query_timeout.py`'s style.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test pins the exact seed-check query text
    execute_cypher_query,
)
from ps_service.query_engine.models import QueryResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]

_TIMEOUT_MS = 5000


class _ScriptedQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted `header`/`result_set` values."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _RowCapRecordingGraphHandle:
    """Satisfies `GraphHandle` structurally -- reports itself as seeded (D11)
    for `_SEED_CHECK_QUERY`, records every query string it was called with
    (AC-BI-007's direct proof site), and returns a scripted oversized/undersized
    result for the caller's own query.
    """

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set
        self.seen_queries: list[str] = []

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _ScriptedQueryResult:
        self.seen_queries.append(q)
        if q == _SEED_CHECK_QUERY:
            return _ScriptedQueryResult(header=[[0, "c"]], result_set=[[1]])
        return _ScriptedQueryResult(header=[[0, "n"]], result_set=self._result_set)


def test_result_exceeding_row_cap_is_truncated_with_truncated_true(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    full_result_set: list[object] = [[value] for value in range(10)]
    fake_graph = _RowCapRecordingGraphHandle(full_result_set)

    result = execute_cypher_query(
        "MATCH (n) RETURN n",
        graph=fake_graph,
        emitter=emitter,
        timeout_ms=_TIMEOUT_MS,
        row_cap=3,
    )

    assert result.rows == [[0], [1], [2]]
    assert result.row_count == 3
    assert result.truncated is True


def test_result_under_row_cap_is_unchanged_with_truncated_false(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    full_result_set: list[object] = [[0], [1]]
    fake_graph = _RowCapRecordingGraphHandle(full_result_set)

    result = execute_cypher_query(
        "MATCH (n) RETURN n",
        graph=fake_graph,
        emitter=emitter,
        timeout_ms=_TIMEOUT_MS,
        row_cap=1000,
    )

    assert result == QueryResult(columns=["n"], rows=[[0], [1]], row_count=2, truncated=False)


def test_result_exactly_at_row_cap_is_not_truncated(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    full_result_set: list[object] = [[0], [1], [2]]
    fake_graph = _RowCapRecordingGraphHandle(full_result_set)

    result = execute_cypher_query(
        "MATCH (n) RETURN n",
        graph=fake_graph,
        emitter=emitter,
        timeout_ms=_TIMEOUT_MS,
        row_cap=3,
    )

    assert result.row_count == 3
    assert result.truncated is False


def test_row_cap_truncation_never_calls_graph_config_set_or_resultset_size(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    full_result_set: list[object] = [[value] for value in range(10)]
    fake_graph = _RowCapRecordingGraphHandle(full_result_set)
    query = "MATCH (n) RETURN n"

    execute_cypher_query(
        query,
        graph=fake_graph,
        emitter=emitter,
        timeout_ms=_TIMEOUT_MS,
        row_cap=3,
    )

    caller_queries = [q for q in fake_graph.seen_queries if q != _SEED_CHECK_QUERY]
    assert caller_queries == [query]
    assert not any("GRAPH.CONFIG" in q or "RESULTSET_SIZE" in q for q in fake_graph.seen_queries)
