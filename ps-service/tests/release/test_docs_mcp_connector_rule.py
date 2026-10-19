"""The docs state the direct-connector rule consistently.

Structural: wording and issue numbers are free. The tests read the real markdown files off
disk (no fakes) and check shape only: no doc ties the direct connector to a Dynamic Client
Registration prerequisite, the reach matrix exists with every row tagged verified or
expected, the user guide links to it, the architecture entries name the `ps-cli` client and
no MCP bridge, and the identity-provider contract defines the other-provider case.
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


def test_architecture_entries_require_ps_cli_client_and_name_no_bridge() -> None:
    cli_entry = _entry("- PS-Cli")
    skill_entry = _entry("- PS Question Skill")
    assert _DCR_PREREQUISITE.findall(cli_entry + skill_entry) == []
    assert "ps-cli" in cli_entry.lower()
    assert "ps-cli" in skill_entry.lower()
    assert "bridge" not in (cli_entry + skill_entry).lower()


def test_idp_contract_defines_other_identity_providers() -> None:
    assert any("Other identity providers" in h for h in _headings(_IDP_CONTRACT))


def test_idp_contract_other_providers_name_federation_and_no_direct_path() -> None:
    section = _section(_IDP_CONTRACT, "### Other identity providers").lower()
    for needle in ("authentik", "federate", "no direct"):
        assert needle in section, f"other-providers section lacks {needle!r}"
    assert "bridge" not in section


_MCP_BRIDGE_REFERENCE = re.compile(
    r"(?i)mcp[ -]?bridge|the bridge|ps-cli-" + "mcp|bridge" + "_credentials"
)


def test_docs_name_no_retired_bridge() -> None:
    offenders = [
        f"{path.relative_to(_REPO)}:{number}"
        for path in sorted((_REPO / "docs").rglob("*.md"))
        for number, line in enumerate(_text(path).splitlines(), 1)
        if _MCP_BRIDGE_REFERENCE.search(line)
    ]
    assert offenders == []


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


# --- Plugin remote-connector docs -------------------------------------------

_BRIDGE = "ps-cli-" + "mcp-bridge"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_PLUGIN_WORDS = re.compile(r"(?i)plugin|ps-mcp|connector")
_CONTEXT_DEPENDENCY = re.compile(r"(?i)context|ps-cli auth login|reuses that login")
_BANNED_STRINGS = (
    "until #173",
    "#173 and #174",
    "#173/#174",
    "mcp-bridge.log",
    "token_resolution_latency",
    "upstream_latency",
    "no stored credentials",
)
_STEP_9 = "## B5. Install the Policy System Plugin"
_USER_PLUGIN_HEADING = "## Using Claude Desktop - Code"
_USER_SETTINGS_HEADING = "### Plugin (`ps-mcp`) settings"
_PS_MCP_URL_ERROR = "Missing environment variables: PS_MCP_URL"


def _section(path: Path, start_heading: str) -> str:
    """Text from `start_heading` to the next heading of the same or higher level."""
    level = len(start_heading) - len(start_heading.lstrip("#"))
    after = _text(path).split(f"{start_heading}\n", 1)
    assert len(after) == 2, f"{path.name} has no section {start_heading!r}"
    end = re.compile(rf"^#{{1,{level}}} ", flags=re.MULTILINE)
    return re.split(end, after[1], maxsplit=1)[0]


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _sentences(path: Path) -> list[str]:
    return _SENTENCE_END.split(_flat(path))


def test_installation_guide_step_9_documents_ps_mcp_url_setup() -> None:
    step = _squash(_section(_INSTALL, _STEP_9))
    for needle in (
        "PS_MCP_URL",
        '"env"',
        "https://ps.test:30443/mcp/",
        _PS_MCP_URL_ERROR,
        "userConfig",
        "prompt=consent",
        "same URL",
        "2.1.236",
        "2.31226.1",
        "2.1.295",
        "untested",
        "53682",
    ):
        assert needle in step, f"installation guide step 9 lacks {needle!r}"


def test_user_guide_documents_ps_mcp_url_and_error() -> None:
    section = _squash(_section(_USER, _USER_PLUGIN_HEADING))
    settings = _squash(_section(_USER, _USER_SETTINGS_HEADING))
    assert "PS_MCP_URL" in section
    assert "./installation-guide.md#b5-install-the-policy-system-plugin" in section
    for needle in (
        "PS_MCP_URL",
        _PS_MCP_URL_ERROR,
        "userConfig",
        "prompt=consent",
        "same URL",
        "2.1.236",
        "2.31226.1",
        "untested",
    ):
        assert needle in settings, f"user guide plugin settings lack {needle!r}"


@pytest.mark.parametrize("guide", [_INSTALL, _USER], ids=lambda p: p.name)
def test_guide_has_no_retired_bridge_notes(guide: Path) -> None:
    flat = _flat(guide)
    assert [s for s in _BANNED_STRINGS if s in flat] == []


@pytest.mark.parametrize("guide", [_INSTALL, _USER], ids=lambda p: p.name)
def test_guide_does_not_say_the_plugin_uses_the_bridge(guide: Path) -> None:
    assert [s for s in _sentences(guide) if "bridge" in s.lower() and _PLUGIN_WORDS.search(s)] == []


@pytest.mark.parametrize("guide", [_INSTALL, _USER], ids=lambda p: p.name)
def test_guide_does_not_tie_the_plugin_to_the_ps_cli_context(guide: Path) -> None:
    assert [
        s for s in _sentences(guide) if _PLUGIN_WORDS.search(s) and _CONTEXT_DEPENDENCY.search(s)
    ] == []


@pytest.mark.parametrize("guide", [_INSTALL, _USER], ids=lambda p: p.name)
def test_guide_does_not_claim_only_the_cli_is_verified(guide: Path) -> None:
    assert [s for s in _sentences(guide) if "only the Claude Code CLI is verified" in s] == []


def test_user_guide_keeps_the_desktop_code_heading_and_matrix_link() -> None:
    assert _USER_PLUGIN_HEADING in _text(_USER)
    assert "](./installation-guide.md#which-claude-surfaces-reach-your-deployment)" in _text(_USER)


def test_reach_matrix_rows_carry_the_recorded_evidence() -> None:
    body = _matrix_body()
    rows = {r[0]: " ".join(r) for r in _matrix_data_rows(body)}
    cli = next(v for k, v in rows.items() if "CLI" in k)
    desktop_code = next(v for k, v in rows.items() if "Code tab" in k)
    chat = next(v for k, v in rows.items() if re.search(r"(?i)chat", k))
    claude_ai = next(v for k, v in rows.items() if "claude.ai" in k)
    assert "2.1.236" in cli
    assert "Verified" in cli
    assert "PS_MCP_URL" in desktop_code
    assert "2.31226.1" in desktop_code
    for needle in ("GO", "custom connector", "ps-cli", "expected"):
        assert needle in chat.lower() or needle in chat, f"Chat/Cowork row lacks {needle!r}"
    assert "expected" in claude_ai.lower()
    assert "untested" in _squash(body)
    assert "oauth.clientId" in body
    assert _BRIDGE not in body
