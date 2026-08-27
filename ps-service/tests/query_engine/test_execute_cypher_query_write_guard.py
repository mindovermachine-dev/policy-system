"""Tests for `execute_cypher_query`'s write-clause guard (AC-002).

PLAN_REVIEWED.md §5, Increment 3: a hand-written fake `GraphHandle` with a
`calls: list[str]` call log proves `.query()` is never invoked for any of
the seven write clauses, upper/lower/mixed case; a word-boundary negative
case proves the regex doesn't false-positive on an identifier substring; an
exact-message-text assertion (Q3 fix) pins the rejection wording so drift is
caught mechanically, not just by manual review; and `is_write_clause` is
also tested directly, since an independent caller (`mcp_interface`) also
goes through it.

Batch 4/Increment 6 wired `_log` into `execute_cypher_query`'s rejected
path, so every call here now needs a live emitter (or a configured process
default) or it raises `LoggingLifecycleError` -- mirrors
`tests/llm_interface/test_route_completion_mocked.py`'s established
throwaway-emitter fixture pattern; these tests don't assert on log content,
only that `execute_cypher_query` itself behaves correctly.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.query_engine import cypher_query
from ps_service.query_engine.cypher_query import execute_cypher_query, is_write_clause
from ps_service.query_engine.errors import WriteClauseRejectedError


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally. Never actually returned
    on the rejection path -- present only so `_FakeGraphHandle.query` has a
    valid return type if it were ever (wrongly) called."""

    def __init__(self) -> None:
        self.header: list[list[object]] = []
        self.result_set: list[object] = []


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally. Records every `.query()`
    invocation in `calls` so tests can assert the guard short-circuited
    before FalkorDB was ever touched."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        return _FakeQueryResult()


_WRITE_CLAUSES = ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "DROP", "FOREACH"]


@pytest.mark.parametrize("clause", _WRITE_CLAUSES)
@pytest.mark.parametrize(
    "case_transform",
    [str.upper, str.lower, lambda s: s[0] + s[1:].lower() if len(s) > 1 else s.lower()],
    ids=["upper", "lower", "mixed"],
)
def test_write_clause_rejected_before_graph_query_called(
    clause: str, case_transform: object, emitter: LogEmitter
) -> None:
    keyword = case_transform(clause)  # type: ignore[operator]
    query = f"{keyword} (n:Thing) RETURN n"
    fake_graph = _FakeGraphHandle()

    with pytest.raises(WriteClauseRejectedError):
        execute_cypher_query(query, graph=fake_graph, emitter=emitter)

    assert fake_graph.calls == []


def test_write_clause_rejected_error_exact_message_text(emitter: LogEmitter) -> None:
    """Q3 fix: pins the exact rejection wording, not just the exception
    type, so wording drift (like the original S1 flaw) is caught
    mechanically by this suite."""
    fake_graph = _FakeGraphHandle()

    with pytest.raises(WriteClauseRejectedError) as excinfo:
        execute_cypher_query("CREATE (n:Thing) RETURN n", graph=fake_graph, emitter=emitter)

    assert str(excinfo.value) == cypher_query._WRITE_CLAUSE_REJECTION_MESSAGE


def test_word_boundary_does_not_false_positive_on_identifier_substring(emitter: LogEmitter) -> None:
    """`CreateEvent` contains the substring `Create` but is not the write
    clause keyword -- the `\\b...\\b` word-boundary regex must not reject
    this query."""
    fake_graph = _FakeGraphHandle()

    result = execute_cypher_query("MATCH (n:CreateEvent) RETURN n", graph=fake_graph, emitter=emitter)

    assert fake_graph.calls == ["MATCH (n:CreateEvent) RETURN n"]
    assert result.row_count == 0


def test_is_write_clause_true_for_representative_write_query() -> None:
    assert is_write_clause("CREATE (n:Thing) RETURN n") is True


def test_is_write_clause_false_for_representative_read_query() -> None:
    assert is_write_clause("MATCH (n) RETURN n") is False
