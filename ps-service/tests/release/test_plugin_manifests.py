"""Plugin and marketplace manifests carry the `ps-*` ids (issue #178, AC-BI-001..003, AC-BI-010).

Reads the real manifest files off disk; no fakes. The marketplace manifest has no display-name
field, so "Policy System Marketplace" lives only in its `description` and in the docs.

The homepage test depends on `docs/artifacts/user-guide.md` keeping its `## Using Claude Desktop`
heading unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

_REPO = Path(__file__).resolve().parents[3]
_MARKETPLACE = _REPO / ".claude-plugin" / "marketplace.json"
_PLUGIN_DIR = _REPO / "ps-skills" / "ps-plugin"
_USER_GUIDE = _REPO / "docs" / "artifacts" / "user-guide.md"


def _load(path: Path) -> dict[str, object]:
    return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))


def _slug(heading: str) -> str:
    h = heading.strip().replace("`", "").lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")


def _heading_slugs(path: Path) -> set[str]:
    slugs: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"^#{1,6} (.+)$", line)
        if match:
            slugs.add(_slug(match.group(1)))
    return slugs


def _plugin_entries() -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", _load(_MARKETPLACE)["plugins"])


def test_marketplace_id_is_ps_marketplace() -> None:
    marketplace = _load(_MARKETPLACE)

    assert marketplace["name"] == "ps-marketplace"
    description = marketplace.get("description")
    assert isinstance(description, str)
    assert "Policy System Marketplace" in description


def test_marketplace_lists_exactly_one_ps_plugin_entry() -> None:
    entries = _plugin_entries()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "ps-plugin"
    assert entry["displayName"] == "Policy System Plugin"
    assert entry["source"] == "./ps-skills/ps-plugin"


def test_marketplace_entry_source_dir_holds_a_plugin_manifest() -> None:
    source = str(_plugin_entries()[0]["source"])

    assert (_REPO / source / ".claude-plugin" / "plugin.json").is_file()


def test_plugin_manifest_name_matches_marketplace_entry() -> None:
    plugin = _load(_PLUGIN_DIR / ".claude-plugin" / "plugin.json")

    assert plugin["name"] == "ps-plugin"
    assert plugin["name"] == _plugin_entries()[0]["name"]
    assert plugin["displayName"] == "Policy System Plugin"


def test_mcp_json_declares_only_the_ps_mcp_server() -> None:
    servers = cast("dict[str, object]", _load(_PLUGIN_DIR / ".mcp.json")["mcpServers"])

    assert set(servers) == {"ps-mcp"}


def test_plugin_homepage_fragment_resolves_in_user_guide() -> None:
    plugin = _load(_PLUGIN_DIR / ".claude-plugin" / "plugin.json")
    homepage = str(plugin["homepage"])

    assert "/docs/artifacts/user-guide.md#" in homepage
    fragment = homepage.partition("#")[2]
    assert fragment in _heading_slugs(_USER_GUIDE)
