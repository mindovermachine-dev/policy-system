"""Tests for the `domain_concepts` MCP tool.

Claude Desktop exposes MCP *tools* to the model but surfaces MCP
*resources* only through its attachment menu, so a model-driven skill
cannot read `psdomain://concepts` on its own. The `domain_concepts` tool
is the slim graph schema (labels, properties, edges) rendered from the
code-defined domain schema: no parameters, no file read, so it keeps
working when the packaged markdown is unavailable (AC-BI-017, tool half).

Hand-written monkeypatching only -- no `unittest.mock`. Server coroutines
are driven with bare `asyncio.run(...)`.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from typing import TYPE_CHECKING

import pytest
from mcp.types import CallToolResult, TextContent

from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from pathlib import Path


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


def test_tool_listed_alongside_cypher() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())

    names = {t.name for t in tools}
    assert {"cypher", "domain_concepts"} <= names
    [tool] = [t for t in tools if t.name == "domain_concepts"]
    assert tool.input_schema.get("required", []) == []


def test_tool_returns_slim_schema_not_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-001/009/017/021: the tool renders from code and ignores the packaged file."""
    missing = tmp_path / "does-not-exist.md"
    _point_helper_at(monkeypatch, missing)

    outcome = _call_tool()

    assert outcome.is_error is False
    text = _text_of(outcome)
    assert not text.startswith("error:")
    assert "Role" in text
    assert "name: string !" in text
    assert "does-not-exist.md" not in text
    assert str(missing) not in text


def test_tool_shows_defines_edge_with_direction_cardinality_and_edge_property() -> None:
    """AC-BI-002/009: the tool output carries the RI -> Role edge from the code schema."""
    text = _text_of(_call_tool())

    assert "(:RegulatoryInstrument)-[:DEFINES {source_ref: string !}]->(:Role)  1 : 0..*" in text


def test_tool_lists_spine_labels_with_enum_values_and_presence_marks() -> None:
    """AC-BI-001/009: spine nodes appear with enum values and required/conditional marks."""
    lines = _text_of(_call_tool()).splitlines()

    for label in ("RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability"):
        assert label in lines
    assert "  source_type: enum(external|internal) !" in lines
    assert "  instrument_type: enum(regulation|directive|national_transposition) ?" in lines
    assert "  status: enum(active|deprecated|merged)" in lines


def test_tool_lists_all_ten_labels_and_policy_proposed_status() -> None:
    """AC-BI-001/009: every domain label is served, and Policy status includes `proposed`."""
    lines = _text_of(_call_tool()).splitlines()

    for label in (
        "RegulatoryInstrument",
        "Role",
        "Requirement",
        "Obligation",
        "PracticeArea",
        "RiskPath",
        "Capability",
        "Policy",
        "Standard",
        "Control",
    ):
        assert label in lines
    assert "  status: enum(draft|proposed|approved|deprecated) !" in lines


def test_tool_output_has_no_markdown_prose_or_notes() -> None:
    """AC-BI-019: the served text is the slim schema: no document headings, tables or notes."""
    text = _text_of(_call_tool())

    assert "##" not in text
    assert "| Property |" not in text
    assert "Lifecycle" not in text
    assert "Short human-readable summary" not in text


_FORBIDDEN_IN_TOOL_BODY = frozenset(
    {
        "read_domain_concepts",
        "_load_domain_concepts",
        "_domain_concepts_path",
        "McpResourceUnavailableError",
    }
)


def _forbidden_names_in_function(source: str, function_name: str) -> set[str]:
    tree = ast.parse(textwrap.dedent(source))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    used = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    used |= {node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)}
    return used & _FORBIDDEN_IN_TOOL_BODY


def test_domain_concepts_tool_body_has_no_file_read() -> None:
    """AC-BI-021: the tool never touches the packaged document or its error class."""
    source = inspect.getsource(mcp_server.domain_concepts)

    assert _forbidden_names_in_function(source, "domain_concepts") == set()


def test_file_read_in_a_tool_body_would_be_detected() -> None:
    source = """
    def domain_concepts():
        try:
            return read_domain_concepts()
        except McpResourceUnavailableError:
            return "error"
    """

    assert _forbidden_names_in_function(source, "domain_concepts") == {
        "read_domain_concepts",
        "McpResourceUnavailableError",
    }


def test_resource_helpers_remain_for_the_resource() -> None:
    """AC-BI-021: the resource still uses the helpers the tool no longer calls."""
    assert callable(mcp_server.read_domain_concepts)
    assert callable(mcp_server._domain_concepts_path)  # pyright: ignore[reportPrivateUsage]  # asserting the helper survives for the resource
    assert issubclass(mcp_server.McpResourceUnavailableError, Exception)
    assert isinstance(mcp_server._DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL, str)  # pyright: ignore[reportPrivateUsage]  # asserting the constant survives for the resource
