"""Tests for the relocated standalone dev CLI, `ps_service.query_engine.
cypher_cli` (PLAN_REVIEWED.md §5, Batch 5 / Increment 7).

Covers:
- `build_parser()` parses `cypher "..."` with the expected defaults.
- `cmd_cypher`'s S2 fix: the write-clause guard runs *before* `_connect`
  is ever called -- proven directly via a hand-written spy on `_connect`,
  not just inferred from AC-002's weaker "never sent to FalkorDB"
  requirement. Q3 fix: the exact rejection stderr text is pinned.
- `cmd_cypher`'s success path, `--format json`, producing the exact JSON
  shape `mcp_server.py`'s `cypher()` tool forwards to its own caller
  verbatim as stdout.
- `cmd_cypher`'s FalkorDB-failure path: `_connect` *was* called (unlike
  the write-clause branch), and the original exception message is
  surfaced verbatim via `error: ...`.

Hand-written structural fakes throughout, per repo convention -- no
`unittest.mock`.
"""

from __future__ import annotations

import argparse
import json

import pytest

from ps_service.logging import configure
from ps_service.query_engine import cypher_cli
from ps_service.query_engine.cypher_query import _WRITE_CLAUSE_REJECTION_MESSAGE


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally. `query()` either returns a
    scripted `_FakeQueryResult` or raises a scripted exception, depending
    on which is configured."""

    def __init__(
        self,
        *,
        result: _FakeQueryResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _ConnectSpy:
    """Hand-written spy standing in for `_connect` -- records every call
    and returns a scripted fake `GraphHandle`, without a real FalkorDB
    connection ever being constructed."""

    def __init__(self, graph: _FakeGraphHandle) -> None:
        self._graph = graph
        self.calls: list[argparse.Namespace] = []

    def __call__(self, args: argparse.Namespace) -> _FakeGraphHandle:
        self.calls.append(args)
        return self._graph


def test_build_parser_parses_cypher_subcommand_with_expected_defaults() -> None:
    parser = cypher_cli.build_parser()

    args = parser.parse_args(["cypher", "MATCH (n) RETURN n"])

    assert args.query == "MATCH (n) RETURN n"
    assert args.host == "localhost"
    assert args.port == 6379
    assert args.graph == "policy_system"
    assert args.format == "text"
    assert args.func is cypher_cli.cmd_cypher


def test_cmd_cypher_rejects_write_clause_before_connect_is_called(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spy = _ConnectSpy(_FakeGraphHandle(result=_FakeQueryResult(header=[], result_set=[])))
    monkeypatch.setattr(cypher_cli, "_connect", spy)

    parser = cypher_cli.build_parser()
    args = parser.parse_args(["cypher", "CREATE (n:Thing) RETURN n"])

    exit_code = cypher_cli.cmd_cypher(args)

    assert exit_code == 1
    assert spy.calls == []
    captured = capsys.readouterr()
    assert captured.err == f"error: {_WRITE_CLAUSE_REJECTION_MESSAGE}\n"
    assert captured.out == ""


def test_cmd_cypher_success_path_prints_exact_json_shape_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `execute_cypher_query` (Batch 4) requires a configured default
    # emitter when `cmd_cypher` doesn't pass one explicitly -- mirrors what
    # `main()` bootstraps via `configure()` in production. `tests/conftest.py`'s
    # autouse `_isolate_logging` fixture redirects PS_LOGGING_DIR to a per-test
    # tmp_path and resets the facade around every test, so this is isolated.
    configure()
    fake_graph = _FakeGraphHandle(
        result=_FakeQueryResult(header=[[0, "id"], [0, "name"]], result_set=[["a", "Alice"], ["b", "Bob"]])
    )
    spy = _ConnectSpy(fake_graph)
    monkeypatch.setattr(cypher_cli, "_connect", spy)

    parser = cypher_cli.build_parser()
    args = parser.parse_args(["cypher", "--format", "json", "MATCH (n) RETURN n.id, n.name"])

    exit_code = cypher_cli.cmd_cypher(args)

    assert exit_code == 0
    assert spy.calls == [args]
    captured = capsys.readouterr()
    expected = json.dumps(
        {"columns": ["id", "name"], "rows": [["a", "Alice"], ["b", "Bob"]], "row_count": 2},
        indent=2,
        default=str,
    )
    assert captured.out == expected + "\n"
    # Confirm the printed payload parses to exactly the shape
    # `mcp_server.py`'s `cypher()` tool forwards verbatim as its own
    # return value (`columns`/`rows`/`row_count`, on success).
    assert json.loads(captured.out) == {
        "columns": ["id", "name"],
        "rows": [["a", "Alice"], ["b", "Bob"]],
        "row_count": 2,
    }


def test_cmd_cypher_falkordb_failure_calls_connect_and_surfaces_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure()  # see comment in the success-path test above
    fake_graph = _FakeGraphHandle(error=RuntimeError("syntax error at offset 4"))
    spy = _ConnectSpy(fake_graph)
    monkeypatch.setattr(cypher_cli, "_connect", spy)

    parser = cypher_cli.build_parser()
    args = parser.parse_args(["cypher", "MATCH (n RETURN n"])

    exit_code = cypher_cli.cmd_cypher(args)

    assert exit_code == 1
    # Unlike the write-clause branch, `_connect` IS called here -- this is
    # what distinguishes the two rejection paths.
    assert spy.calls == [args]
    captured = capsys.readouterr()
    assert captured.err == "error: syntax error at offset 4\n"
    assert captured.out == ""
