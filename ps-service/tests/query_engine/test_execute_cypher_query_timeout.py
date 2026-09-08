"""Tests for `execute_cypher_query`'s timeout enforcement (AC-BI-004, AC-BI-005, AC-BI-009).

PLAN.md §4 S2 / D1 / D2: `timeout_ms` is threaded straight into the single
`graph.query(query, timeout=timeout_ms)` call site (the caller's own query,
never `_is_graph_seeded`'s own separate `graph.query` call). FalkorDB has no
dedicated timeout exception type (PLAN.md §2.5), so a server-side abort is
classified by a message-substring check (`_classify_execution_failure`,
D2) -- `"timed out"` (case-insensitive) in the exception's own message means
`outcome="timeout"`, anything else means `outcome="failed"` -- without ever
logging the raw exception message or the raw query text.

`row_cap` is accepted but not yet load-bearing in this slice (S3 adds
truncation) -- every test here uses a `row_cap` value larger than any
scripted result set so it is a no-op.

Hand-written structural fakes throughout, per repo convention (no
`unittest.mock`), mirroring `test_execute_cypher_query_logging.py`'s style.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test pins the exact seed-check query text
    execute_cypher_query,
)
from ps_service.query_engine.errors import QueryEngineExecutionError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]
    type ReadLines = Callable[[Path], list[dict[str, object]]]

_TIMEOUT_MS = 5000
_ROW_CAP = 1000


class _ScriptedQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted `header`/`result_set` values."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _TimeoutRecordingGraphHandle:
    """Satisfies `GraphHandle` structurally -- reports itself as seeded (D11)
    for `_SEED_CHECK_QUERY`, and records the `timeout=` kwarg it was called
    with for the caller's own query.
    """

    def __init__(self) -> None:
        self.seen_timeouts: list[int | None] = []

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _ScriptedQueryResult:
        if q == _SEED_CHECK_QUERY:
            return _ScriptedQueryResult(header=[[0, "c"]], result_set=[[1]])
        self.seen_timeouts.append(timeout)
        return _ScriptedQueryResult(header=[[0, "n"]], result_set=[["a"]])


class _RaisingGraphHandle:
    """Satisfies `GraphHandle` structurally -- reports itself as seeded (D11)
    for `_SEED_CHECK_QUERY`, and raises a scripted exception for the caller's
    own query.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _ScriptedQueryResult:
        if q == _SEED_CHECK_QUERY:
            return _ScriptedQueryResult(header=[[0, "c"]], result_set=[[1]])
        raise self._exc


def _assert_no_query_text_or_exception_message_logged(
    entry: dict[str, object], query: str, exception_message: str
) -> None:
    """Direct positive check: neither the query text nor the exception's own
    message ever appears anywhere in the serialized entry (mirrors
    `test_execute_cypher_query_logging.py::_assert_no_query_text_logged`,
    extended to also cover the exception message D2's classification reads
    but must never persist).
    """
    serialized = json.dumps(entry)
    assert query not in serialized
    assert query not in entry.values()
    assert exception_message not in serialized
    assert exception_message not in entry.values()


def test_timeout_ms_passed_to_graph_query_as_native_timeout_kwarg(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    fake_graph = _TimeoutRecordingGraphHandle()

    execute_cypher_query(
        "MATCH (n) RETURN n",
        graph=fake_graph,
        emitter=emitter,
        timeout_ms=_TIMEOUT_MS,
        row_cap=_ROW_CAP,
    )

    assert fake_graph.seen_timeouts == [_TIMEOUT_MS]


def test_query_exceeding_timeout_raises_query_engine_execution_error_with_message(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    fake_graph = _RaisingGraphHandle(RuntimeError("Query timed out"))

    with pytest.raises(QueryEngineExecutionError) as excinfo:
        execute_cypher_query(
            "MATCH (n) RETURN n",
            graph=fake_graph,
            emitter=emitter,
            timeout_ms=_TIMEOUT_MS,
            row_cap=_ROW_CAP,
        )

    assert str(excinfo.value) == "Query timed out"


def test_timeout_logs_outcome_timeout_not_failed(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    fake_graph = _RaisingGraphHandle(RuntimeError("Query timed out"))

    with pytest.raises(QueryEngineExecutionError):
        execute_cypher_query(
            "MATCH (n) RETURN n",
            graph=fake_graph,
            emitter=emitter,
            timeout_ms=_TIMEOUT_MS,
            row_cap=_ROW_CAP,
        )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["outcome"] == "timeout"


def test_non_timeout_failure_still_logs_outcome_failed(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    fake_graph = _RaisingGraphHandle(RuntimeError("syntax error at offset 4"))

    with pytest.raises(QueryEngineExecutionError):
        execute_cypher_query(
            "MATCH (n) RETURN n",
            graph=fake_graph,
            emitter=emitter,
            timeout_ms=_TIMEOUT_MS,
            row_cap=_ROW_CAP,
        )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["outcome"] == "failed"


def test_timeout_log_entry_never_contains_raw_exception_message_or_query_text(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    query = "MATCH (n {ssn: '123-45-6789'}) RETURN n"
    exception_message = "Query timed out after 5000ms: MATCH (n {ssn: '123-45-6789'}) RETURN n"
    fake_graph = _RaisingGraphHandle(RuntimeError(exception_message))

    with pytest.raises(QueryEngineExecutionError):
        execute_cypher_query(
            query,
            graph=fake_graph,
            emitter=emitter,
            timeout_ms=_TIMEOUT_MS,
            row_cap=_ROW_CAP,
        )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["outcome"] == "timeout"
    _assert_no_query_text_or_exception_message_logged(entry, query, exception_message)
