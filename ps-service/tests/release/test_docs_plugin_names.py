"""The guides a user follows name the `ps-*` plugin, marketplace and connector (issue #178).

Reads the real markdown files off disk; no fakes. The old connector names are built by
concatenation so the repo-wide AC-BI-008 grep stays empty. Depends on the user-guide heading
`## Using Claude Desktop` staying unchanged (the plugin homepage fragment points at it).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

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

_REPO = Path(__file__).resolve().parents[3]
_ARTIFACTS = _REPO / "docs" / "artifacts"
_INSTALL = _ARTIFACTS / "installation-guide.md"
_USER = _ARTIFACTS / "user-guide.md"
_OPS = _ARTIFACTS / "operations-guide.md"
_SKILLS_README = _REPO / "ps-skills" / "readme.md"
_GUIDES = (_INSTALL, _USER, _OPS)
_LINKED_FILES = (*_GUIDES, _SKILLS_README)

_INSTALL_CMD = "/plugin install ps-plugin@ps-marketplace"
_REQUIRED_NAMES = ("Policy System Marketplace", "Policy System Plugin", "Policy System MCP")
_PLUGIN_ANCHOR = "9-install-the-policy-system-plugin"
_RESET_HEADING = "Reset an earlier account-scoped marketplace (manual)"
_UPDATE_HEADING = "Updating or reinstalling the Policy System Plugin"
_LINK = re.compile(r"\]\((?:\./([\w.-]+\.md))?#([^)\s]+)\)")

# Fragments that cannot be retargeted; empty means every link in the four files resolves.
_KNOWN_BROKEN: frozenset[tuple[str, str]] = frozenset()


def _slug(heading: str) -> str:
    h = heading.strip().replace("`", "").lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def _heading_slugs(path: Path) -> set[str]:
    seen: Counter[str] = Counter()
    slugs: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"^#{1,6} (.+)$", line)
        if match:
            base = _slug(match.group(1))
            slugs.add(base if seen[base] == 0 else f"{base}-{seen[base]}")
            seen[base] += 1
    return slugs


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("guide", _GUIDES, ids=lambda p: p.name)
def test_guide_names_the_marketplace_plugin_connector_and_install_command(guide: Path) -> None:
    text = _text(guide)
    missing = [name for name in (*_REQUIRED_NAMES, _INSTALL_CMD) if name not in text]
    assert not missing, f"{guide.name} lacks {missing}"


@pytest.mark.parametrize("guide", _GUIDES, ids=lambda p: p.name)
def test_guide_has_no_old_connector_names(guide: Path) -> None:
    text = _text(guide)
    assert [p for p in OLD_PATTERNS if p in text] == []


@pytest.mark.parametrize("doc", _LINKED_FILES, ids=lambda p: p.name)
def test_every_markdown_fragment_link_resolves(doc: Path) -> None:
    broken: list[str] = []
    for match in _LINK.finditer(_text(doc)):
        target = doc.parent / match.group(1) if match.group(1) else doc
        fragment = match.group(2)
        if (doc.name, fragment) in _KNOWN_BROKEN:
            continue
        if not target.exists() or fragment not in _heading_slugs(target):
            broken.append(match.group(0))
    assert broken == []


def test_install_guide_toc_entry_and_links_to_plugin_step_resolve() -> None:
    install = _text(_INSTALL)
    assert f"](#{_PLUGIN_ANCHOR})" in install
    assert _PLUGIN_ANCHOR in _heading_slugs(_INSTALL)
    assert f"./installation-guide.md#{_PLUGIN_ANCHOR})" in _text(_USER)


def test_install_guide_group5_reset_subsection_has_resolving_toc_entry() -> None:
    install = _text(_INSTALL)
    anchor = _slug(_RESET_HEADING)
    assert f"### {_RESET_HEADING}" in install
    assert f"](#{anchor})" in install
    assert anchor in _heading_slugs(_INSTALL)


def test_install_guide_group5_lists_marketplaces_without_naming_the_old_remove_target() -> None:
    text = _text(_INSTALL)
    assert "claude plugin marketplace list" in text
    assert "claude plugin marketplace remove <" in text


def test_unverified_claude_naming_is_marked_expected() -> None:
    assert "verify with `claude mcp list`" in _text(_INSTALL)


def test_operations_guide_update_subsection_has_resolving_toc_entry() -> None:
    ops = _text(_OPS)
    anchor = _slug(_UPDATE_HEADING)
    assert f"### {_UPDATE_HEADING}" in ops
    assert f"](#{anchor})" in ops
    assert "claude plugin update ps-plugin@ps-marketplace" in ops


def test_skills_readme_gives_the_install_command() -> None:
    assert _INSTALL_CMD in _text(_SKILLS_README)


def test_architecture_doc_has_no_old_connector_names() -> None:
    text = _text(_REPO / "docs" / "architecture" / "ps-service-container-architecture.md")
    assert [p for p in OLD_PATTERNS if p in text] == []
    assert "`policy-system` plugin" not in text
