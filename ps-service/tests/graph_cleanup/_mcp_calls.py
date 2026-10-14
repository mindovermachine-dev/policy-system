"""Call the registered MCP tools of the real server as a given caller (continuity tests).

The continuity tests drive each cleanup flow through the MCP tool that previews it, the signing
router, the executor and the tools that report on it. This is the small shared piece of that:
authenticate as a caller, call a tool, get its text.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


@contextlib.contextmanager
def caller(subject: str, issuer: str) -> Generator[None]:
    """Run the enclosed MCP calls as the authenticated `(subject, issuer)`."""
    token = AccessToken(
        token="t", client_id="c", scopes=[], subject=subject, claims={"iss": issuer}
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def call_tool(name: str, args: dict[str, object], who: tuple[str, str]) -> str:
    """Call the MCP tool `name` as `who`; return the text of its single result block."""
    with caller(*who):
        result = asyncio.run(mcp_server.server.call_tool(name, args))
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def factory(value: object) -> Callable[..., object]:
    """A constructor stand-in that ignores its arguments and returns `value`."""

    def _make(*_args: object, **_kwargs: object) -> object:
        return value

    return _make
