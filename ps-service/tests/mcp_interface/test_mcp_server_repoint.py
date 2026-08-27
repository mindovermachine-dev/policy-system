"""Tests for `mcp_server.py`'s repoint of `CYPHER_CLI` at
`query_engine/cypher_cli.py` (PLAN_REVIEWED.md §5, Batch 6 / Increment 8).

Covers:
- (a) `CYPHER_CLI` resolves into `query_engine/`, not the deleted
  `mcp_interface/cypher_cli.py`.
- (b) the real command-building path inside the `cypher()` tool uses the
  repointed constant -- proven by capturing the `cmd` list passed to
  `subprocess.run` via a hand-written fake, not `unittest.mock`.
- (c) (Q1 fix) the old `mcp_interface/cypher_cli.py` file no longer exists
  on disk -- the one assertion in the whole suite that directly proves
  AC-003's "guard/execution logic exists in exactly one place" by proving
  the old file is actually gone, not just that the new path exists.
- (d) (N1 fix) the client-facing `MCPServer(instructions=...)` string no
  longer names the stale `mcp_interface/cypher_cli.py` path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ps_service.mcp_interface import mcp_server


class _FakeCompletedProcess:
    """Satisfies the small slice of `subprocess.CompletedProcess` that
    `cypher()` actually reads (`returncode`, `stdout`, `stderr`)."""

    def __init__(self, *, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_cypher_cli_constant_points_at_query_engine() -> None:
    """(a) `CYPHER_CLI` resolves into `query_engine/`, by name and by
    parent directory, imported directly -- no subprocess run, no live
    server needed."""
    assert mcp_server.CYPHER_CLI.name == "cypher_cli.py"
    assert mcp_server.CYPHER_CLI.parent.name == "query_engine"


def test_cypher_tool_builds_subprocess_cmd_against_repointed_constant(monkeypatch) -> None:
    """(b) The real `cypher()` tool's command-building path uses the
    repointed `CYPHER_CLI` constant -- captured via a hand-written fake
    standing in for `subprocess.run`, not `unittest.mock`."""
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], *, capture_output: bool, text: bool, timeout: int) -> _FakeCompletedProcess:
        captured["cmd"] = cmd
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["timeout"] = timeout
        return _FakeCompletedProcess(
            returncode=0,
            stdout='{"columns": [], "rows": [], "row_count": 0}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = mcp_server.cypher("MATCH (n) RETURN n")

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[1] == str(mcp_server.CYPHER_CLI)
    assert captured["timeout"] == 60
    assert result == '{"columns": [], "rows": [], "row_count": 0}'


def test_old_mcp_interface_cypher_cli_no_longer_exists() -> None:
    """(c) (Q1 fix) `ps_service/mcp_interface/cypher_cli.py` -- the file
    Batch 5 relocated to `query_engine/cypher_cli.py` -- no longer exists
    on disk. This is the one test in the whole suite that directly proves
    AC-003's "exactly one place" property; it must actually run, not be
    silently skipped.
    """
    old_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "ps_service"
        / "mcp_interface"
        / "cypher_cli.py"
    )
    assert not old_path.exists()


def test_instructions_string_does_not_name_stale_path() -> None:
    """(d) (N1 fix) The client-facing `MCPServer(instructions=...)` string
    no longer names the deleted `mcp_interface/cypher_cli.py` path.

    Checking for the literal old substring `"mcp_interface/cypher_cli.py"`
    (rather than the bare word `"cypher_cli.py"`) is the more precise,
    less brittle check here: the instructions string is free to mention
    `cypher_cli.py` by name as long as it isn't naming a path that no
    longer resolves to anything (the actual failure mode N1 was raised
    against), and this repo's revised string in fact drops the filename
    entirely in favor of "the underlying read-only Cypher CLI" -- so this
    assertion holds either way the wording is phrased, without being tied
    to one exact sentence.
    """
    instructions = mcp_server.server.instructions
    assert instructions is not None
    assert "mcp_interface/cypher_cli.py" not in instructions
    assert "cypher_cli.py" not in instructions
    # Still meaningfully describes the guard behavior.
    assert "write clause" in instructions.lower()
