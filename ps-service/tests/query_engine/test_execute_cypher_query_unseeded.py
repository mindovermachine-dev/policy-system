"""Tests for `execute_cypher_query`'s unseeded-graph guard (D11, AC-BI-013/AC-BI-014).

Slice 8.1 (PLAN.md Batch 8): a new pre-flight check, `_is_graph_seeded`,
issues one cheap `count(n)` read before the caller's own query is ever sent.
A fake `GraphHandle` scripted with an ordered response queue proves the
guard ordering end to end via call-count/call-content assertions on the
fake, not by inspecting `execute_cypher_query`'s internals: an unseeded
graph (`count(n) = 0`) raises `GraphUnseededError` after exactly one
`.query()` call (the seed check) and the caller's query never runs; a
seeded graph (`count(n) > 0`) runs the caller's query normally as a second
call. A write-clause query against an unseeded graph still raises
`WriteClauseRejectedError` first, with zero `.query()` calls at all --
proving the pre-existing write-clause guard (cheap, no I/O) is not
reordered behind the new (one-I/O) seed check.

Mirrors `test_execute_cypher_query_write_guard.py`'s fake/emitter-fixture
conventions -- every call needs a live emitter or it raises
`LoggingLifecycleError` (Batch 4/Increment 6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.logging.emitter import EmitterConfig, LogEmitter
from ps_service.query_engine.cypher_query import (
    _SEED_CHECK_QUERY,  # pyright: ignore[reportPrivateUsage]  # test pins the exact seed-check query text
    execute_cypher_query,
)
from ps_service.query_engine.errors import GraphUnseededError, WriteClauseRejectedError

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
    `calls` -- an empty `calls` (or a `calls` shorter than expected) proves
    the caller's own query was never sent to FalkorDB. Popping past the end
    of the script raises `IndexError`, which fails a test cleanly if more
    `.query()` calls happen than the test expects.
    """

    def __init__(self, responses: list[_FakeQueryResult]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        return self._responses.pop(0)


def test_unseeded_graph_raises_before_callers_query_runs(emitter: LogEmitter) -> None:
    fake = _ScriptedGraphHandle([_FakeQueryResult(header=[[0, "c"]], result_set=[[0]])])

    with pytest.raises(GraphUnseededError):
        execute_cypher_query("MATCH (n:Thing) RETURN n", graph=fake, emitter=emitter)

    assert fake.calls == [_SEED_CHECK_QUERY]


def test_seeded_graph_runs_callers_query_normally(emitter: LogEmitter) -> None:
    fake = _ScriptedGraphHandle(
        [
            _FakeQueryResult(header=[[0, "c"]], result_set=[[3]]),
            _FakeQueryResult(header=[[0, "n"]], result_set=[["a"]]),
        ]
    )

    result = execute_cypher_query("MATCH (n:Thing) RETURN n", graph=fake, emitter=emitter)

    assert fake.calls == [_SEED_CHECK_QUERY, "MATCH (n:Thing) RETURN n"]
    assert result.row_count == 1


def test_write_clause_on_unseeded_graph_still_rejected_first_with_zero_io(
    emitter: LogEmitter,
) -> None:
    """Guard ordering: the pre-existing write-clause guard (cheap, no I/O)
    must not be reordered behind the new (one-I/O) seed check. No responses
    are scripted at all -- if the seed check ran first, `.query()` would be
    called and `.pop(0)` on the empty list would raise `IndexError` instead
    of the expected `WriteClauseRejectedError`.
    """
    fake = _ScriptedGraphHandle([])

    with pytest.raises(WriteClauseRejectedError):
        execute_cypher_query("CREATE (n:Thing) RETURN n", graph=fake, emitter=emitter)

    assert fake.calls == []
