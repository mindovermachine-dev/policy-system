"""Every registered MCP tool is named lower-kebab-case with a documented verb first.

The verb set is the table under "Tool naming convention" in the container architecture document,
and this file parses it, so the documented vocabulary and the enforced one cannot drift apart. The
meta-tests feed the same finder deliberately misnamed tools to prove it reports them (precedent:
`test_gate_declarations.py`).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Iterable

_ARCHITECTURE = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "architecture"
    / "ps-service-container-architecture.md"
)
_CONVENTION_HEADING = "#### Tool naming convention"
_VERB_ROW = re.compile(r"^\| `([a-z]+)` \|", re.MULTILINE)
_KEBAB_NAME = re.compile(r"[a-z]+(-[a-z]+)+")


def documented_verbs(document: Path = _ARCHITECTURE) -> frozenset[str]:
    """The verbs listed in the document's "Tool naming convention" table."""
    text = document.read_text(encoding="utf-8")
    assert _CONVENTION_HEADING in text, f"{document.name} has no '{_CONVENTION_HEADING}' section"
    section = text.split(_CONVENTION_HEADING, 1)[1].split("\n#### ", 1)[0]
    verbs = frozenset(_VERB_ROW.findall(section))
    assert verbs, f"the '{_CONVENTION_HEADING}' section lists no verbs"
    return verbs


def find_convention_violations(names: Iterable[str], verbs: frozenset[str]) -> list[str]:
    """Names that are not lower-kebab-case of two or more words, or whose first word is no verb."""
    violations: list[str] = []
    for name in names:
        if _KEBAB_NAME.fullmatch(name) is None or name.split("-", 1)[0] not in verbs:
            violations.append(name)
    return violations


_REGISTERED_TOOL_NAMES = sorted(tool.name for tool in asyncio.run(mcp_server.server.list_tools()))
_VERBS = frozenset({"get", "list", "check"})


def test_a_snake_case_name_is_reported() -> None:
    assert find_convention_violations(["get_thing"], _VERBS) == ["get_thing"]


def test_a_bare_word_is_reported() -> None:
    assert find_convention_violations(["list"], _VERBS) == ["list"]


def test_an_uppercase_name_is_reported() -> None:
    assert find_convention_violations(["Get-Thing"], _VERBS) == ["Get-Thing"]


def test_a_name_whose_first_word_is_not_a_documented_verb_is_reported() -> None:
    assert find_convention_violations(["thing-list"], _VERBS) == ["thing-list"]


def test_a_kebab_name_with_a_documented_verb_is_accepted() -> None:
    assert find_convention_violations(["list-things", "check-thing-status"], _VERBS) == []


def test_a_document_without_the_convention_section_fails_loudly(tmp_path: Path) -> None:
    document = tmp_path / "architecture.md"
    document.write_text("# Architecture\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="Tool naming convention"):
        documented_verbs(document)


def test_the_documented_verb_set_is_not_empty() -> None:
    assert documented_verbs()


@pytest.mark.parametrize("name", _REGISTERED_TOOL_NAMES)
def test_registered_tool_name_follows_the_convention(name: str) -> None:
    assert find_convention_violations([name], documented_verbs()) == []
