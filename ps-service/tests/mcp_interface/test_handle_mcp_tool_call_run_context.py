"""Tests for run-correlation in `mcp_server.handle_mcp_tool_call`
(PLAN_REVIEWED.md §6, Batch 3).

Covers AC-006 (a fresh `run_id` is bound via `bind_run_context` before the
delegate runs), AC-007 (the delegate's emitted log entry carries that
`run_id`; no log entry contains the raw query text) and AC-008 (a second
call gets its own distinct `run_id`).

Real `execute_cypher_query` + a hand-written `GraphHandle` fake that records
`current_run_id()` at `query()` time; a real `LogEmitter` writing to a tmp
JSONL read back via the root-conftest `make_emitter` / `read_lines`
fixtures. No `unittest.mock`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.logging.run_context import current_run_id
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]
    type ReadLines = Callable[[Path], list[dict[str, object]]]


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted values."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _RunIdRecordingGraphHandle:
    """Satisfies `GraphHandle` structurally. Records the `run_id` bound in
    the calling context every time `query()` is invoked.
    """

    def __init__(self) -> None:
        self.seen_run_ids: list[str | None] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.seen_run_ids.append(current_run_id())
        return _FakeQueryResult(header=[[0, "n"]], result_set=[["x"]])


def test_run_id_bound_and_visible_to_delegate(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    fake = _RunIdRecordingGraphHandle()

    mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)
    emitter.flush()

    assert len(fake.seen_run_ids) == 1
    run_id = fake.seen_run_ids[0]
    assert isinstance(run_id, str)
    assert run_id != ""


def test_two_calls_get_distinct_run_ids(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    fake = _RunIdRecordingGraphHandle()

    mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)
    mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)
    emitter.flush()

    assert len(fake.seen_run_ids) == 2
    assert fake.seen_run_ids[0] != fake.seen_run_ids[1]


def test_emitted_log_entry_carries_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    fake = _RunIdRecordingGraphHandle()

    mcp_server.handle_mcp_tool_call("MATCH (n) RETURN n", graph=fake, emitter=emitter)
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no log entry was written -- wiring bug"
    entry = lines[-1]
    assert entry["action"] == "execute_cypher_query"
    assert entry["run_id"] == fake.seen_run_ids[0]


def test_no_emitted_log_entry_contains_query_text(make_emitter: MakeEmitter) -> None:
    emitter, log_path = make_emitter()
    fake = _RunIdRecordingGraphHandle()
    query = "MATCH (n {ssn: '123-45-6789'}) RETURN n"

    mcp_server.handle_mcp_tool_call(query, graph=fake, emitter=emitter)
    emitter.flush()

    raw = log_path.read_text(encoding="utf-8")
    assert raw != "", "no log entry was written -- wiring bug"
    assert "123-45-6789" not in raw
