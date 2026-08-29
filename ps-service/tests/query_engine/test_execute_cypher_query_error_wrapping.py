"""Tests for `execute_cypher_query`'s FalkorDB-failure wrapping.

PLAN_REVIEWED.md §5, Increment 5: a fake `GraphHandle.query` that raises
proves `execute_cypher_query` re-raises as `QueryEngineExecutionError` with
the original exception's message preserved verbatim, chained via
`raise ... from exc` so `__cause__` is the original exception.

Batch 4/Increment 6 wired `_log` into `execute_cypher_query`'s failure path,
so every call here now needs a live emitter (or a configured process
default) or it raises `LoggingLifecycleError` -- mirrors
`tests/llm_interface/test_route_completion_mocked.py`'s established
throwaway-emitter fixture pattern; these tests don't assert on log content,
only that `execute_cypher_query` itself behaves correctly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import pytest

from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.query_engine.cypher_query import execute_cypher_query
from ps_service.query_engine.errors import QueryEngineExecutionError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def emitter(tmp_path: Path) -> Iterator[LogEmitter]:
    log_emitter = LogEmitter(EmitterConfig(log_path=tmp_path / "test.jsonl"))
    yield log_emitter
    log_emitter.stop()


class _RaisingGraphHandle:
    """Satisfies `GraphHandle` structurally -- `.query()` always raises the
    scripted exception instead of returning a result. Annotated `NoReturn`
    (it never actually returns) so it satisfies `GraphHandle`'s `query(...)
    -> GraphQueryResult` structurally under strict type checking.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def query(self, q: str, params: dict[str, object] | None = None) -> NoReturn:
        raise self._exc


def test_graph_query_failure_wrapped_as_query_engine_execution_error_with_verbatim_message(
    emitter: LogEmitter,
) -> None:
    original = RuntimeError("connection refused: FalkorDB unreachable")
    fake_graph = _RaisingGraphHandle(original)

    with pytest.raises(QueryEngineExecutionError) as excinfo:
        execute_cypher_query("MATCH (n) RETURN n", graph=fake_graph, emitter=emitter)

    assert str(excinfo.value) == "connection refused: FalkorDB unreachable"


def test_graph_query_failure_chains_original_exception_as_cause(emitter: LogEmitter) -> None:
    original = RuntimeError("syntax error at offset 12")
    fake_graph = _RaisingGraphHandle(original)

    with pytest.raises(QueryEngineExecutionError) as excinfo:
        execute_cypher_query("MATCH (n) RETURN n", graph=fake_graph, emitter=emitter)

    assert excinfo.value.__cause__ is original
