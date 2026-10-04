"""Every shipped skill names the single `ps-mcp` connector and none of the old ones (issue #178).

Walks every Markdown file under `ps-skills/ps-plugin/` (AC-BI-003, AC-BI-005). The old names are
built by concatenation so this file never spells them itself (the repo-wide rename gate is a
plain grep).
"""

from __future__ import annotations

import re
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[3] / "ps-skills" / "ps-plugin"

_OLD_GRAPH = "policy-system" + "-graph"
_OLD_MARKETPLACE = "policy-system" + "-marketplace"
_OLD_SKILLS_DIR = "ps-skills/" + "policy-system"
_OLD_INSTALL = "plugin install " + "policy-system"
_OLD_LISTING = "plugin:" + "policy-system"
_OLD_TOOLPREFIX = "plugin_" + "policy-system"
OLD_PATTERNS = (
    _OLD_GRAPH,
    _OLD_MARKETPLACE,
    _OLD_SKILLS_DIR,
    _OLD_INSTALL,
    _OLD_LISTING,
    _OLD_TOOLPREFIX,
)

_FORBIDDEN = re.compile(r"graph-local|user-registered")
_TOOL_LITERAL = re.compile(r"mcp__plugin_\S+")
_TOOL_OK = re.compile(r"mcp__plugin_ps-plugin_ps-mcp__[a-z_]+")


def _markdown_files() -> list[Path]:
    return sorted(_PLUGIN_DIR.rglob("*.md"))


def _skill_files() -> list[Path]:
    return sorted((_PLUGIN_DIR / "skills").glob("*/SKILL.md"))


def test_plugin_tree_has_twelve_skills() -> None:
    assert len(_skill_files()) == 12


def test_no_markdown_names_an_old_pattern() -> None:
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for pattern in OLD_PATTERNS:
            assert pattern not in text, f"{path.relative_to(_PLUGIN_DIR)} still has an old name"
        assert _FORBIDDEN.search(text) is None, (
            f"{path.relative_to(_PLUGIN_DIR)} still describes a local-connector fallback"
        )


def test_every_skill_names_the_ps_mcp_connector() -> None:
    for path in _skill_files():
        assert "ps-mcp" in path.read_text(encoding="utf-8"), (
            f"{path.parent.name} never names the ps-mcp connector"
        )


def test_any_plugin_tool_literal_has_the_exact_new_form() -> None:
    for path in _markdown_files():
        for literal in _TOOL_LITERAL.findall(path.read_text(encoding="utf-8")):
            stripped = literal.rstrip(".,;:)`'\"")
            assert _TOOL_OK.fullmatch(stripped), f"{path.name}: unexpected tool literal {stripped}"
