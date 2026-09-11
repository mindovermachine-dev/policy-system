"""Tests for the `domain_concepts` MCP tool.

Claude Desktop exposes MCP *tools* to the model but surfaces MCP
*resources* only through its attachment menu, so a model-driven skill
cannot read `psdomain://concepts` on its own. The `domain_concepts` tool
is the same content behind a tool call: verbatim markdown, no
parameters, and -- matching the `cypher` tool's convention -- a clean
`error:` string rather than a raised exception when the backing file is
unreadable, with no filesystem detail crossing the MCP boundary.

Hand-written monkeypatching only -- no `unittest.mock`. Server coroutines
are driven with bare `asyncio.run(...)`.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING

import pytest
from mcp.types import CallToolResult, TextContent

from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from pathlib import Path

_KNOWN_MARKDOWN = "# PS domain concepts\n\nRegulation → Obligation -- café ✅\n"


@pytest.fixture(autouse=True)
def clear_domain_concepts_path_cache() -> None:
    mcp_server._domain_concepts_path.cache_clear()  # pyright: ignore[reportPrivateUsage]  # test reaches into a module-internal cached helper by design


def _point_helper_at(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(mcp_server, "_domain_concepts_path", lambda: path)


def _call_tool() -> CallToolResult:
    outcome = asyncio.run(mcp_server.server.call_tool("domain_concepts", {}))
    assert isinstance(outcome, CallToolResult)
    return outcome


def _text_of(outcome: CallToolResult) -> str:
    texts = [c.text for c in outcome.content if isinstance(c, TextContent)]
    assert len(texts) == 1
    return texts[0]


def test_tool_takes_no_parameters() -> None:
    assert inspect.signature(mcp_server.domain_concepts).parameters == {}


def test_tool_returns_same_content_as_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    md_file = tmp_path / "ps-domain-concepts.md"
    md_file.write_text(_KNOWN_MARKDOWN, encoding="utf-8")
    _point_helper_at(monkeypatch, md_file)

    assert mcp_server.domain_concepts() == _KNOWN_MARKDOWN
    assert mcp_server.domain_concepts() == mcp_server.read_domain_concepts()


def test_tool_listed_alongside_cypher() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())

    names = {t.name for t in tools}
    assert {"cypher", "domain_concepts"} <= names
    [tool] = [t for t in tools if t.name == "domain_concepts"]
    assert tool.input_schema.get("required", []) == []


def test_call_tool_via_server_returns_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    md_file = tmp_path / "ps-domain-concepts.md"
    md_file.write_text(_KNOWN_MARKDOWN, encoding="utf-8")
    _point_helper_at(monkeypatch, md_file)

    outcome = _call_tool()

    assert outcome.is_error is False
    assert _text_of(outcome) == _KNOWN_MARKDOWN


def test_missing_file_is_clean_error_string_not_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    outcome = _call_tool()

    assert outcome.is_error is False  # an `error:` value, never a ToolError
    text = _text_of(outcome)
    assert text.startswith("error: ")
    assert "does-not-exist.md" not in text
    assert str(missing) not in text
    assert "Errno" not in text
    assert "Traceback" not in text
