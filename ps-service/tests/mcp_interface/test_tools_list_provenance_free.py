"""The served MCP `tools/list` payload, server instructions and resource descriptions carry no
provenance text.

The provenance patterns are the single definition kept by `scripts/check_docstring_provenance.py`;
they are loaded from that script by path. Tool descriptions are the tool function docstrings, so a
reference to an internal planning artefact there would reach every MCP client. The walk covers
every tool name, title and description plus every nested `description` and `title` string of the
input and output schemas. Resource bodies are not read, only their descriptions.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    import types
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "check_docstring_provenance.py"
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SCHEMA_PROSE_KEYS = frozenset({"description", "title"})
_MIN_TOOL_COUNT = 38
_PRIVATE_CALL = re.compile(r"\b_[a-z]\w*\(")
_STORE_OR_GATEWAY = re.compile(r"\b\w+(?:Store|Gateway)\b")
# Client-visible class names that may appear in a tool description, with the reason.
_ALLOWED_CLIENT_TERMS: frozenset[str] = frozenset()


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_docstring_provenance", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _strings(value: object, path: str) -> Iterator[tuple[str, str]]:
    """Yield `(path, text)` for every string under a prose key at any depth of `value`."""
    if isinstance(value, dict):
        for key, item in cast("dict[str, object]", value).items():
            if key in _SCHEMA_PROSE_KEYS and isinstance(item, str):
                yield f"{path}.{key}", item
            else:
                yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(cast("list[object]", value)):
            yield from _strings(item, f"{path}[{index}]")


def _violations(texts: list[tuple[str, str]]) -> list[str]:
    patterns = _load_script().PATTERNS
    found: list[str] = []
    for path, text in texts:
        cleaned = _URL.sub("", text)
        for pattern in patterns:
            match = pattern.regex.search(cleaned)
            if match is not None:
                found.append(f"{path}: {pattern.pattern_id}: {match.group(0)}")
    return found


def _tool_texts() -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for tool in asyncio.run(mcp_server.server.list_tools()):
        payload = tool.model_dump(by_alias=True, exclude_none=True)
        for key in ("name", "title", "description"):
            value = payload.get(key)
            if isinstance(value, str):
                texts.append((f"{tool.name}.{key}", value))
        for schema_key in ("inputSchema", "outputSchema"):
            texts.extend(_strings(payload.get(schema_key), f"{tool.name}.{schema_key}"))
    return texts


def test_tools_list_is_nonempty() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())

    assert len(tools) >= _MIN_TOOL_COUNT
    assert all(tool.description for tool in tools)


def test_tools_list_payload_is_provenance_free() -> None:
    assert _violations(_tool_texts()) == []


def test_server_instructions_and_resource_descriptions_are_provenance_free() -> None:
    texts: list[tuple[str, str]] = []
    instructions = mcp_server.server.instructions
    assert instructions
    texts.append(("instructions", instructions))
    resources = asyncio.run(mcp_server.server.list_resources())
    assert resources
    texts.extend(
        (f"resource {resource.name}", resource.description)
        for resource in resources
        if resource.description
    )

    assert _violations(texts) == []


def test_matcher_flags_known_violating_descriptions() -> None:
    ac_id = "AC" + "-BI-" + "003"
    issue = "issue " + "#" + "12"
    plan = "PLA" + "N.md"
    spike = "spi" + "ke-3"
    samples = [
        (f"sample{i}", f"Lists items per {text}.")
        for i, text in enumerate([ac_id, issue, plan, spike])
    ]

    flagged = _violations(samples)

    assert len(flagged) >= len(samples)
    assert {line.split(":")[0] for line in flagged} == {name for name, _ in samples}


def test_matcher_passes_clean_description_and_strips_urls() -> None:
    clean = [
        ("clean", "Lists the ingested instruments; see https://example.com/issues/#12 for more.")
    ]

    assert _violations(clean) == []


def test_tool_descriptions_name_no_internals() -> None:
    offenders: list[str] = []
    for tool in asyncio.run(mcp_server.server.list_tools()):
        text = tool.description or ""
        if "ps_service." in text:
            offenders.append(f"{tool.name}: module path")
        if _PRIVATE_CALL.search(text):
            offenders.append(f"{tool.name}: private identifier")
        offenders.extend(
            f"{tool.name}: {m.group(0)}"
            for m in _STORE_OR_GATEWAY.finditer(text)
            if m.group(0) not in _ALLOWED_CLIENT_TERMS
        )

    assert offenders == []
