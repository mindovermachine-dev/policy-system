"""The docs state the direct-connector rule consistently (issue #170).

Structural: wording and issue numbers are free. The tests read the real markdown files off
disk (no fakes) and check shape only: no doc ties the direct connector to a Dynamic Client
Registration prerequisite, the reach matrix exists with every row tagged verified or
expected, the user guide links to it, the architecture entries name the `ps-cli` client and
the legacy bridge, and the identity-provider contract defines the other-provider case.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_ARTIFACTS = _REPO / "docs" / "artifacts"
_ARCHITECTURE_DIR = _REPO / "docs" / "architecture"
_ARCHITECTURE = _ARCHITECTURE_DIR / "ps-solution-architecture.md"
_INSTALL = _ARTIFACTS / "installation-guide.md"
_USER = _ARTIFACTS / "user-guide.md"
_OPS = _ARTIFACTS / "operations-guide.md"
_IDP_CONTRACT = _ARTIFACTS / "idp-configuration-contract.md"
_SKILLS_README = _REPO / "ps-skills" / "readme.md"
_UX_DESIGN = _ARCHITECTURE_DIR / "ps-client-ux-design.md"
_NATIVE_EXPLORATION = _ARCHITECTURE_DIR / "ps-native-client-exploration.md"

_GUIDES = (_INSTALL, _USER, _OPS)
_RULE_DOCS = (
    _ARCHITECTURE,
    *_GUIDES,
    _IDP_CONTRACT,
    _SKILLS_README,
    _UX_DESIGN,
    _NATIVE_EXPLORATION,
)
_FRAGMENT_DOCS = (_ARCHITECTURE, _IDP_CONTRACT)

_DCR_PREREQUISITE = re.compile(
    r"(?i)(IdPs?|identity providers?)\s+(that\s+)?"
    r"(supports?|supporting|lacks?|without)\s+(dynamic client registration|dcr)"
)
_FRAGMENT_LINK = re.compile(r"\]\((?:(\.{1,2}/)?([\w./-]+\.md))?#([^)\s]+)\)")
_BARE_MD_FRAGMENT_LINK = re.compile(r"\]\((?!\.{1,2}/)(?!https?:)([\w./-]+\.md)#[^)\s]+\)")
_MATRIX_HEADING = "Which Claude surfaces reach"
_UNVERIFIED_SURFACE = re.compile(r"(?i)chat|cowork|claude\.ai")
_MIN_MATRIX_ROWS = 4


def _slug(heading: str) -> str:
    h = heading.strip().replace("`", "").lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def _headings(path: Path) -> list[str]:
    found: list[str] = []
    in_fence = False
    for line in _text(path).splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"^#{1,6} (.+)$", line)
        if match:
            found.append(match.group(1))
    return found


def _heading_slugs(path: Path) -> set[str]:
    seen: Counter[str] = Counter()
    slugs: set[str] = set()
    for heading in _headings(path):
        base = _slug(heading)
        slugs.add(base if seen[base] == 0 else f"{base}-{seen[base]}")
        seen[base] += 1
    return slugs


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", _text(path))


def _matrix_heading() -> str | None:
    return next((h for h in _headings(_INSTALL) if _MATRIX_HEADING in h), None)


def _matrix_body() -> str:
    heading = _matrix_heading()
    assert heading is not None, "installation guide has no reach-matrix section"
    after = _text(_INSTALL).split(f"## {heading}\n", 1)[1]
    return re.split(r"^## ", after, maxsplit=1, flags=re.MULTILINE)[0]


def _matrix_data_rows(body: str) -> list[list[str]]:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in body.splitlines()
        if line.lstrip().startswith("|")
    ]
    return [row for row in rows[1:] if not set("".join(row)) <= set("-: ")]


def _entry(prefix: str) -> str:
    lines = [ln for ln in _text(_ARCHITECTURE).splitlines() if ln.startswith(prefix)]
    assert lines, f"no architecture entry starting {prefix!r}"
    return re.sub(r"\s+", " ", lines[0])


@pytest.mark.parametrize("doc", _RULE_DOCS, ids=lambda p: p.name)
def test_doc_does_not_tie_direct_connector_to_dynamic_client_registration(doc: Path) -> None:
    assert _DCR_PREREQUISITE.findall(_flat(doc)) == []


def test_installation_guide_has_reach_matrix_with_every_row_tagged() -> None:
    body = _matrix_body()
    rows = _matrix_data_rows(body)
    lowered = body.lower()
    assert len(rows) >= _MIN_MATRIX_ROWS
    assert [r[0] for r in rows if not re.search(r"(?i)verified|expected", " ".join(r))] == []
    assert [
        r[0]
        for r in rows
        if _UNVERIFIED_SURFACE.search(r[0])
        and re.search(r"(?i)verified", re.sub(r"(?i)not yet verified", "", " ".join(r)))
        and "expected" not in " ".join(r).lower()
    ] == []
    assert "consent" in lowered
    assert "same url" in lowered


def test_user_guide_links_to_reach_matrix() -> None:
    heading = _matrix_heading()
    assert heading is not None
    assert f"](./installation-guide.md#{_slug(heading)})" in _text(_USER)


def test_architecture_entries_require_ps_cli_client_and_mark_bridge_legacy() -> None:
    cli_entry = _entry("- PS-Cli")
    skill_entry = _entry("- PS Question Skill")
    assert _DCR_PREREQUISITE.findall(cli_entry + skill_entry) == []
    assert "ps-cli" in cli_entry.lower()
    assert "ps-cli" in skill_entry.lower()
    assert "legacy" in skill_entry.lower()


def test_idp_contract_defines_other_identity_providers() -> None:
    assert any("Other identity providers" in h for h in _headings(_IDP_CONTRACT))


def _broken_fragment_links(doc: Path) -> list[str]:
    broken: list[str] = []
    for match in _FRAGMENT_LINK.finditer(_text(doc)):
        prefix, relative_file, fragment = match.groups()
        target = doc.parent / (prefix or "") / relative_file if relative_file else doc
        if not target.exists() or fragment not in _heading_slugs(target):
            broken.append(match.group(0))
    return broken


@pytest.mark.parametrize("doc", _FRAGMENT_DOCS, ids=lambda p: p.name)
def test_fragment_links_resolve_in_architecture_and_idp_contract(doc: Path) -> None:
    assert _broken_fragment_links(doc) == []


@pytest.mark.parametrize("guide", _GUIDES, ids=lambda p: p.name)
def test_guides_link_to_other_documents_with_dot_slash_form(guide: Path) -> None:
    assert _BARE_MD_FRAGMENT_LINK.findall(_text(guide)) == []
