"""The draft tools' published descriptions make no `error:` string promise for malformed input."""

from __future__ import annotations

import asyncio

from ps_service.mcp_interface import mcp_server

_DRAFT_TOOLS: tuple[str, ...] = (
    "create-policy-draft",
    "update-policy-draft",
    "add-standard-to-draft",
    "update-standard-draft",
    "add-control-to-draft",
    "update-control-draft",
)
_FORBIDDEN_PHRASES: tuple[str, ...] = (
    "malformed",
    "error: fields",
    "error: standards",
    "error: control_type",
)


def test_tools_list_descriptions_make_no_error_string_promise_for_malformed_input() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.server.list_tools())}

    for name in _DRAFT_TOOLS:
        description = (tools[name].description or "").lower()
        assert description, name
        for phrase in _FORBIDDEN_PHRASES:
            assert phrase not in description, f"{name} description still mentions {phrase!r}"
