"""Every tool rejects an argument its published input schema does not declare, before it runs.

The calls go through `mcp.client.Client`, because a direct `server.call_tool(...)` skips the
server middleware chain where the check lives.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from mcp.client import Client
from mcp.types import TextContent

from mcp_interface._environment import fail_closed_environment
from ps_service.logging import configure
from ps_service.mcp_interface.mcp_server import build_server

if TYPE_CHECKING:
    from mcp.types import CallToolResult

    from ps_service.mcp_interface.mcp_server import BuiltServer


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _built() -> BuiltServer:
    return build_server(
        fail_closed_environment(audit_store_factory=audit_store_factory(InMemoryAuditStore()))
    )


async def _call(built: BuiltServer, name: str, arguments: dict[str, object]) -> CallToolResult:
    async with Client(built.server) as client:
        return await client.call_tool(name, arguments)


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list-near-misses", {"unexpected": True}),
        ("resolve-near-miss", {"review_id": "r1", "decision": "keep-separate", "extra": 1}),
    ],
)
def test_an_unknown_argument_is_rejected_for_any_tool(
    tool: str, arguments: dict[str, object]
) -> None:
    result = asyncio.run(_call(_built(), tool, arguments))

    assert result.is_error is True
    assert "unknown argument" in _text(result)
    assert "extra" in _text(result) or "unexpected" in _text(result)


def test_an_unknown_argument_never_enters_the_tool_body() -> None:
    audit = InMemoryAuditStore()
    built = build_server(fail_closed_environment(audit_store_factory=audit_store_factory(audit)))

    result = asyncio.run(
        _call(built, "resolve-near-miss", {"review_id": "r1", "decision": "keep-separate", "x": 1})
    )

    assert result.is_error is True
    assert audit.rows == []


def test_declared_arguments_still_run_the_tool() -> None:
    result = asyncio.run(_call(_built(), "list-near-misses", {}))

    assert "unknown argument" not in _text(result)
    assert _text(result).startswith("error: ")


def test_the_published_input_schemas_are_unchanged_by_strict_arguments() -> None:
    built = _built()

    async def _schemas() -> dict[str, object]:
        return {tool.name: tool.input_schema for tool in await built.server.list_tools()}

    with_check = asyncio.run(_schemas())
    built.server.middleware.clear()
    without_check = asyncio.run(_schemas())

    assert with_check == without_check
    assert with_check
