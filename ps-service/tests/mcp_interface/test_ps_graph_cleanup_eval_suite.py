"""The plugin eval suite for reviewing and splitting automatic resolutions is well formed.

`claude plugin eval` rejects unknown frontmatter keys and makes an unmocked MCP tool
unavailable, so this pins the suite to the documented keys and grader types, to tools the
PS Service really registers, and to a mock for every `ps-mcp` tool a case reaches. Running the
suite needs live model calls and is not part of this test.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import cast

import yaml

from ps_service.mcp_interface import mcp_server

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "ps-skills" / "ps-plugin"
_EVALS = _PLUGIN / "evals"
_SERVER = "ps-mcp"
_TOOL_PREFIX = "mcp__plugin_ps-plugin_ps-mcp__"
_CASES = ("review-split-governed", "review-split-ungoverned")

_PROMPT_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "description",
        "tags",
        "plugins",
        "runs",
        "expected_outcome",
        "model",
        "max_turns",
        "timeout_seconds",
        "allowed_tools",
        "append_system_prompt",
        "env",
    }
)
_GRADER_KEYS = {
    "regex": {"pattern", "flags", "match", "target"},
    "tool_used": {"tool", "input_match", "min", "max"},
    "tool_order": {"before", "after"},
    "llm": {"criteria", "focus"},
}
_COMMON_GRADER_KEYS = {"type", "weight", "arm"}
_MOCK_KEYS = frozenset({"type", "expect", "error", "abort_when"})
_SKILL_TOOLS = (
    "find-capability-merge-candidates",
    "list-capability-resolutions",
    "split-capability",
    "check-cleanup-approval",
)
_REGISTERED = frozenset(tool.name for tool in asyncio.run(mcp_server.server.list_tools()))


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _split(path: Path) -> tuple[dict[str, object], str]:
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), path
    _, front, body = raw.split("---\n", 2)
    return _mapping(yaml.safe_load(front)), body.strip()


def _mock(case: str, tool: str) -> Path:
    own = _EVALS / case / "mocks" / _SERVER / f"{tool}.md"
    return own if own.is_file() else _EVALS / "mocks" / _SERVER / f"{tool}.md"


def _mock_body(case: str, tool: str) -> dict[str, object]:
    _, body = _split(_mock(case, tool))
    return _mapping(json.loads(body))


def _only_resolution(listing: dict[str, object]) -> dict[str, object]:
    resolutions = listing["resolutions"]
    assert isinstance(resolutions, list)
    [entry] = cast("list[object]", resolutions)
    return _mapping(entry)


def _tool_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if value is None:
        return []
    return [str(_mapping(value)["tool"])]


def test_the_mock_server_is_the_connector_the_plugin_declares() -> None:
    declared = json.loads((_PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]

    assert _SERVER in declared
    assert {path.name for path in (_EVALS / "mocks").iterdir()} == {_SERVER}


def test_every_case_has_a_prompt_with_documented_keys_and_at_least_one_grader() -> None:
    cases = sorted(path.name for path in _EVALS.iterdir() if (path / "prompt.md").is_file())

    assert cases == sorted(_CASES)
    for case in _CASES:
        front, body = _split(_EVALS / case / "prompt.md")
        assert set(front) <= _PROMPT_KEYS, case
        assert front["name"] == case
        assert front["runs"] == 1
        assert "cap_x_vuln_patch" in body
        assert sorted((_EVALS / case / "graders").glob("*.md")), case


def test_every_grader_uses_a_documented_type_and_names_registered_tools() -> None:
    for grader in sorted(_EVALS.glob("*/graders/*.md")):
        front, body = _split(grader)
        kind = str(front["type"])
        assert kind in _GRADER_KEYS, grader
        assert set(front) <= _GRADER_KEYS[kind] | _COMMON_GRADER_KEYS, grader
        assert bool(body) == (kind == "llm"), grader
        for key in ("tool", "before", "after"):
            for name in _tool_names(front.get(key)):
                assert name.startswith(_TOOL_PREFIX), grader
                assert name.removeprefix(_TOOL_PREFIX) in _REGISTERED, grader
        if "pattern" in front:
            re.compile(str(front["pattern"]))


def test_every_mock_answers_a_registered_tool_with_documented_keys_and_json() -> None:
    mocks = sorted(_EVALS.glob(f"**/mocks/{_SERVER}/*.md"))

    assert mocks
    for mock in mocks:
        assert mock.stem in _REGISTERED, mock
        front, body = _split(mock)
        assert set(front) <= _MOCK_KEYS, mock
        json.loads(body)


def test_every_tool_the_split_flow_reaches_is_mocked_for_every_case() -> None:
    for case in _CASES:
        for tool in _SKILL_TOOLS:
            assert _mock(case, tool).is_file(), (case, tool)


def test_the_governed_case_offers_no_approval_until_acknowledged() -> None:
    split = _mock_body("review-split-governed", "split-capability")
    listing = _mock_body("review-split-governed", "list-capability-resolutions")

    assert split["acknowledgment_required"] is True
    assert "approval_url" not in split
    assert "pending_approval_id" not in split
    policy = _only_resolution(listing)["governing_policy"]
    assert isinstance(policy, dict)
    assert policy["id"] == "pol_patching"
    front, _ = _split(_mock("review-split-governed", "split-capability"))
    assert front["expect"] == {"x_id": "cap_x_vuln_patch"}


def test_the_ungoverned_case_returns_the_approval_the_graders_look_for() -> None:
    split = _mock_body("review-split-ungoverned", "split-capability")
    listing = _mock_body("review-split-ungoverned", "list-capability-resolutions")
    url_grader, _ = _split(_EVALS / "review-split-ungoverned" / "graders" / "url-given.md")

    preview = split["preview"]
    assert isinstance(preview, dict)
    assert preview["governance"] is None
    assert re.search(str(url_grader["pattern"]), str(split["approval_url"]))
    assert _only_resolution(listing)["governing_policy"] is None


def test_eval_results_are_git_ignored() -> None:
    ignored = (_REPO / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "ps-skills/ps-plugin/evals/results/" in ignored
