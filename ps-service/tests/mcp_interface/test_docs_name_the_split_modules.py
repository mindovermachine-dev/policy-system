"""The docs that used to point at the single `mcp_server.py` name the modules that replaced it.

A whole-tree docs lint (a deliberate exception to the one-to-one `tests/` to `src/` mirror). The
checks are driven by the files on disk, so a new module without a documentation row fails here.
`docs/audits/` is historical and is not scanned.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ps_service.mcp_interface as mcp_package

_REPO = Path(__file__).resolve().parents[3]
_MI = Path(mcp_package.__file__).parent
_ARCHITECTURE = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_LEVEL2 = _REPO / "docs" / "coding-standards" / "level2-python-instructions.md"
_THREAT_MAP = _REPO / "docs" / "secops" / "threat-coverage-map.md"
_AUTHOR_SKILL = _REPO / "ps-skills" / "ps-plugin" / "skills" / "ps-author-policy" / "SKILL.md"
_MI_REPO_PATH = "ps-service/src/ps_service/mcp_interface"
_MODULE_STEMS = sorted(path.stem for path in _MI.glob("*.py") if path.stem != "__init__")
_GROUP_AND_CORE_FILES = sorted([path.name for path in _MI.glob("*_tools.py")] + ["tool_core.py"])


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _architecture_registration() -> str:
    text = _ARCHITECTURE.read_text(encoding="utf-8")
    mcp = text.split("\n### MCP Interface\n", 1)[1]
    return _section(mcp, "#### Implementation Registration", "\n#### Actions")


def _architecture_authorization() -> str:
    text = _ARCHITECTURE.read_text(encoding="utf-8")
    return _section(text, "\n#### MCP tool authorization\n", "\n---\n")


@pytest.mark.parametrize("stem", _MODULE_STEMS)
def test_architecture_registration_has_a_row_for_every_mcp_interface_module(stem: str) -> None:
    assert f"| `{_MI_REPO_PATH}/{stem}.py` |" in _architecture_registration()


@pytest.mark.parametrize("filename", _GROUP_AND_CORE_FILES)
def test_architecture_authorization_section_names_every_tool_module(filename: str) -> None:
    assert f"`{filename}`" in _architecture_authorization()


def test_architecture_front_door_row_no_longer_claims_to_register_the_tools_itself() -> None:
    (row,) = [
        line
        for line in _architecture_registration().splitlines()
        if line.startswith(f"| `{_MI_REPO_PATH}/mcp_server.py` |")
    ]

    assert "front door" in row
    assert "handle_mcp_tool_call" in row
    assert "_run_mcp_action" not in row


def test_architecture_curated_source_row_names_the_catalog_tools_module() -> None:
    text = _ARCHITECTURE.read_text(encoding="utf-8")
    (row,) = [
        line
        for line in text.splitlines()
        if line.startswith("| `ps-service/src/ps_service/curated_source/store.py`")
    ]

    assert "mcp_interface/catalog_tools.py" in row
    assert "mcp_server.py" not in row


@pytest.mark.parametrize(
    ("path", "required"),
    [
        (_LEVEL2, "cypher_tools.py"),
        (_THREAT_MAP, "tool_core.py"),
        (_THREAT_MAP, "_tools.py"),
        (_AUTHOR_SKILL, "policy_lifecycle_tools.py"),
        (_AUTHOR_SKILL, "cypher_tools.py"),
    ],
    ids=lambda value: value.name if isinstance(value, Path) else value,
)
def test_pointer_docs_name_the_new_modules(path: Path, required: str) -> None:
    assert required in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [_LEVEL2, _THREAT_MAP, _AUTHOR_SKILL], ids=lambda p: p.name)
def test_pointer_docs_do_not_name_the_front_door_as_the_home_of_tools(path: Path) -> None:
    assert re.findall(r"`?mcp_server\.py`?", path.read_text(encoding="utf-8")) == []
