"""Tests for `execute_cypher_query`'s structured logging (AC-004).

PLAN_REVIEWED.md §5, Increment 6: with a `run_id` bound via
`bind_run_context`, `execute_cypher_query` emits one structured log entry
per call on every branch (succeeded/rejected/failed), carrying the bound
`run_id`; without a bound run context, `run_id` is absent/`None` and the
call does not crash. Every emitted entry's `extra` must never contain the
raw query text (S1/§0.5's PII-safety deviation from `route_completion`'s own
`extra={"model": ...}` precedent) -- checked as a direct positive assertion,
not just absence-by-omission.

Uses the repo's real `make_emitter`/`read_lines` fixtures (`tests/
conftest.py`) -- a real `LogEmitter` writing JSON lines to a temp file, read
back and parsed -- and hand-written structural fakes for `GraphHandle`, per
repo convention (no `unittest.mock`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn

import pytest

from ps_service.logging import bind_run_context
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test pins the exact seed-check query text
    execute_cypher_query,
)
from ps_service.query_engine.errors import (
    QueryEngineExecutionError,
    WriteClauseRejectedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]
    type ReadLines = Callable[[Path], list[dict[str, object]]]


class _ScriptedQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted `header`/
    `result_set` values.
    """

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeSuccessGraphHandle:
    """Satisfies `GraphHandle` structurally -- reports itself as seeded
    (D11) for `_SEED_CHECK_QUERY`, and otherwise always succeeds with a
    scripted two-row result.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> _ScriptedQueryResult:
        if q == _SEED_CHECK_QUERY:
            return _ScriptedQueryResult(header=[[0, "c"]], result_set=[[1]])
        return _ScriptedQueryResult(
            header=[[0, "id"], [0, "name"]],
            result_set=[["a", "Alice"], ["b", "Bob"]],
        )


class _FakeRaisingGraphHandle:
    """Satisfies `GraphHandle` structurally -- `.query()` always raises."""

    def query(self, q: str, params: dict[str, object] | None = None) -> NoReturn:
        raise RuntimeError("connection refused: FalkorDB unreachable")


_SUCCESS_QUERY = "MATCH (n) RETURN n.id, n.name"
_WRITE_QUERY = "CREATE (n:Thing) RETURN n"
_FAILING_QUERY = "MATCH (n) RETURN n.id, n.name"


def _assert_no_query_text_logged(entry: dict[str, object], query: str) -> None:
    """Direct positive check: the query string never appears anywhere in the
    serialized entry (note: `LogEntry.to_json_line` merges `extra`'s pairs
    directly into the top-level payload -- there is no nested `"extra"` key
    to check separately, so this substring check over the whole serialized
    entry is the correct, exhaustive form of the check).
    """
    serialized = json.dumps(entry)
    assert query not in serialized
    assert query not in entry.values()


def test_success_emits_entry_with_bound_run_id_and_succeeded_outcome(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    fake_graph = _FakeSuccessGraphHandle()

    with bind_run_context("run-x"):
        execute_cypher_query(_SUCCESS_QUERY, graph=fake_graph, emitter=emitter)
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["component"] == "query_engine"
    assert entry["action"] == "execute_cypher_query"
    assert entry["run_id"] == "run-x"
    assert entry["outcome"] == "succeeded"
    _assert_no_query_text_logged(entry, _SUCCESS_QUERY)


def test_rejected_emits_entry_with_bound_run_id_and_rejected_outcome(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    fake_graph = _FakeSuccessGraphHandle()

    with bind_run_context("run-x"), pytest.raises(WriteClauseRejectedError):
        execute_cypher_query(_WRITE_QUERY, graph=fake_graph, emitter=emitter)
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["component"] == "query_engine"
    assert entry["action"] == "execute_cypher_query"
    assert entry["run_id"] == "run-x"
    assert entry["outcome"] == "rejected"
    _assert_no_query_text_logged(entry, _WRITE_QUERY)


def test_failed_emits_entry_with_bound_run_id_and_failed_outcome(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    fake_graph = _FakeRaisingGraphHandle()

    with bind_run_context("run-x"), pytest.raises(QueryEngineExecutionError):
        execute_cypher_query(_FAILING_QUERY, graph=fake_graph, emitter=emitter)
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["component"] == "query_engine"
    assert entry["action"] == "execute_cypher_query"
    assert entry["run_id"] == "run-x"
    assert entry["outcome"] == "failed"
    _assert_no_query_text_logged(entry, _FAILING_QUERY)


def test_no_bound_run_context_run_id_absent_and_no_crash(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    fake_graph = _FakeSuccessGraphHandle()

    execute_cypher_query(_SUCCESS_QUERY, graph=fake_graph, emitter=emitter)
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry.get("run_id") is None


def test_success_with_principal_includes_principal_on_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Slice 3 (issue #67, supports AC-BI-008): an opaque `principal` string,
    when given, is attached to the succeeded-branch log entry.
    """
    emitter, log_path = make_emitter()
    fake_graph = _FakeSuccessGraphHandle()

    execute_cypher_query(
        _SUCCESS_QUERY, graph=fake_graph, emitter=emitter, principal="local-test-bypass"
    )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["outcome"] == "succeeded"
    assert entry["principal"] == "local-test-bypass"


def test_rejected_with_principal_includes_principal_on_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Slice 3 (issue #67, supports AC-BI-008): `principal` is attached on
    the rejected branch too, not only on success.
    """
    emitter, log_path = make_emitter()
    fake_graph = _FakeSuccessGraphHandle()

    with pytest.raises(WriteClauseRejectedError):
        execute_cypher_query(
            _WRITE_QUERY, graph=fake_graph, emitter=emitter, principal="local-test-bypass"
        )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["outcome"] == "rejected"
    assert entry["principal"] == "local-test-bypass"


def test_failed_with_principal_includes_principal_on_entry(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Slice 3 (issue #67, supports AC-BI-008): `principal` is attached on
    the failed branch too, not only on success.
    """
    emitter, log_path = make_emitter()
    fake_graph = _FakeRaisingGraphHandle()

    with pytest.raises(QueryEngineExecutionError):
        execute_cypher_query(
            _FAILING_QUERY, graph=fake_graph, emitter=emitter, principal="local-test-bypass"
        )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert entry["outcome"] == "failed"
    assert entry["principal"] == "local-test-bypass"


def test_no_principal_given_omits_principal_key(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Slice 3 (issue #67, supports AC-BI-008): the default `principal=None`
    stays silent -- no `"principal"` key at all -- so no pre-existing test's
    implicit "extra has no unexpected keys" expectation breaks.
    """
    emitter, log_path = make_emitter()
    fake_graph = _FakeSuccessGraphHandle()

    execute_cypher_query(_SUCCESS_QUERY, graph=fake_graph, emitter=emitter)
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written -- wiring bug"
    entry = lines[-1]
    assert "principal" not in entry
