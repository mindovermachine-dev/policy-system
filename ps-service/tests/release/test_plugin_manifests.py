"""Plugin and marketplace manifests carry the `ps-*` ids (issue #178, AC-BI-001..003, AC-BI-010).

Reads the real manifest files off disk; no fakes. The marketplace manifest has no display-name
field, so "Policy System Marketplace" lives only in its `description` and in the docs.

The homepage test depends on `docs/artifacts/user-guide.md` keeping its `## Using Claude Desktop`
heading unchanged.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

_REPO = Path(__file__).resolve().parents[3]
_MARKETPLACE = _REPO / ".claude-plugin" / "marketplace.json"
_PLUGIN_DIR = _REPO / "ps-skills" / "ps-plugin"
_USER_GUIDE = _REPO / "docs" / "artifacts" / "user-guide.md"
_MCP_JSON = _PLUGIN_DIR / ".mcp.json"
_PS_SKILLS = _REPO / "ps-skills"
_RETIRED_BRIDGE_NAME = "ps-cli-" + "mcp-bridge"
_PS_MCP_KEYS = {"type", "url", "oauth"}
_FORBIDDEN_KEY_PARTS = ("secret", "token", "headers", "authorization", "headershelper")


def _load(path: Path) -> dict[str, object]:
    return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))


def _ps_mcp_violations(mcp_json: dict[str, object]) -> list[str]:
    """Return the ways a `.mcp.json` departs from the remote-http `ps-mcp` declaration."""
    servers = cast("dict[str, dict[str, object]]", mcp_json.get("mcpServers", {}))
    violations: list[str] = []
    if set(servers) != {"ps-mcp"}:
        violations.append(f"servers must be exactly ['ps-mcp'], got {sorted(servers)}")
    server = servers.get("ps-mcp", {})
    if server.get("type") != "http":
        violations.append(f"type must be 'http', got {server.get('type')!r}")
    if server.get("url") != "${PS_MCP_URL}":
        violations.append(f"url must be '${{PS_MCP_URL}}', got {server.get('url')!r}")
    if not isinstance(server.get("oauth"), dict):
        violations.append("oauth block is missing or not an object")
    if set(server) != _PS_MCP_KEYS:
        violations.append(f"ps-mcp keys must be {sorted(_PS_MCP_KEYS)}, got {sorted(server)}")
    return violations


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


def test_mcp_json_declares_ps_mcp_as_remote_http_with_env_url() -> None:
    mcp_json = _load(_MCP_JSON)
    server = cast("dict[str, object]", cast("dict[str, object]", mcp_json["mcpServers"])["ps-mcp"])

    assert _ps_mcp_violations(mcp_json) == []
    assert server["type"] == "http"
    assert server["url"] == "${PS_MCP_URL}"


def test_mcp_json_has_no_stdio_server_and_no_hardcoded_host() -> None:
    raw = _MCP_JSON.read_text(encoding="utf-8")
    server = cast(
        "dict[str, object]", cast("dict[str, object]", _load(_MCP_JSON)["mcpServers"])["ps-mcp"]
    )

    assert "command" not in server
    assert server.get("type") != "stdio"
    assert re.search(r"https?://", raw) is None
    assert _RETIRED_BRIDGE_NAME not in raw


def test_ps_mcp_oauth_block_is_exactly_the_pre_registered_ps_cli_client() -> None:
    server = cast(
        "dict[str, object]", cast("dict[str, object]", _load(_MCP_JSON)["mcpServers"])["ps-mcp"]
    )
    oauth = cast("dict[str, object]", server["oauth"])

    assert oauth == {"clientId": "ps-cli", "callbackPort": 53682}
    assert isinstance(oauth["callbackPort"], int)


_GOOD_DECLARATION: dict[str, object] = {
    "mcpServers": {
        "ps-mcp": {
            "type": "http",
            "url": "${PS_MCP_URL}",
            "oauth": {"clientId": "ps-cli", "callbackPort": 53682},
        }
    }
}


def _mutate_second_server(mcp_json: dict[str, object]) -> None:
    servers = cast("dict[str, object]", mcp_json["mcpServers"])
    servers["other"] = copy.deepcopy(servers["ps-mcp"])


def _mutate_rename_key(mcp_json: dict[str, object]) -> None:
    servers = cast("dict[str, object]", mcp_json["mcpServers"])
    servers["ps"] = servers.pop("ps-mcp")


def _ps_mcp(mcp_json: dict[str, object]) -> dict[str, object]:
    return cast("dict[str, object]", cast("dict[str, object]", mcp_json["mcpServers"])["ps-mcp"])


def _mutate_type_stdio(mcp_json: dict[str, object]) -> None:
    _ps_mcp(mcp_json)["type"] = "stdio"


def _mutate_type_sse(mcp_json: dict[str, object]) -> None:
    _ps_mcp(mcp_json)["type"] = "sse"


def _mutate_literal_host(mcp_json: dict[str, object]) -> None:
    _ps_mcp(mcp_json)["url"] = "https://ps.example.com/mcp/"


def _mutate_other_variable(mcp_json: dict[str, object]) -> None:
    _ps_mcp(mcp_json)["url"] = "${OTHER}"


def _mutate_delete_oauth(mcp_json: dict[str, object]) -> None:
    del _ps_mcp(mcp_json)["oauth"]


def _mutate_add_command(mcp_json: dict[str, object]) -> None:
    _ps_mcp(mcp_json)["command"] = "ps-cli-bridge"


def _mutate_add_headers(mcp_json: dict[str, object]) -> None:
    _ps_mcp(mcp_json)["headers"] = {"Authorization": "x"}


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate_second_server,
        _mutate_rename_key,
        _mutate_type_stdio,
        _mutate_type_sse,
        _mutate_literal_host,
        _mutate_other_variable,
        _mutate_delete_oauth,
        _mutate_add_command,
        _mutate_add_headers,
    ],
)
def test_guard_flags_a_bad_declaration(mutate: Callable[[dict[str, object]], None]) -> None:
    mcp_json = copy.deepcopy(_GOOD_DECLARATION)
    assert _ps_mcp_violations(mcp_json) == []

    mutate(mcp_json)

    assert _ps_mcp_violations(mcp_json) != []


def test_plugin_files_carry_no_secret_token_or_auth_header_keys() -> None:
    ps_mcp_keys = {key.lower() for key in _ps_mcp(_load(_MCP_JSON))}
    plugin_json = _load(_PLUGIN_DIR / ".claude-plugin" / "plugin.json")
    plugin_keys = {key.lower() for key in plugin_json}

    for key in ps_mcp_keys | plugin_keys:
        assert not any(part in key for part in _FORBIDDEN_KEY_PARTS), key
    assert "mcpServers" not in plugin_json
    assert "oauth" not in plugin_json
    for path in (_MCP_JSON, _PLUGIN_DIR / ".claude-plugin" / "plugin.json"):
        raw = path.read_text(encoding="utf-8")
        assert "Bearer " not in raw
        assert "Authorization" not in raw


def test_no_ps_skills_file_mentions_the_retired_bridge_script() -> None:
    offenders: list[str] = []
    for path in sorted(_PS_SKILLS.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _RETIRED_BRIDGE_NAME in text:
            offenders.append(str(path.relative_to(_REPO)))

    assert offenders == []
