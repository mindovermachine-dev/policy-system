"""Package front door for the MCP Interface.

Re-exports the component's sanitised boundary exception types and the injectable
`handle_mcp_tool_call` core. The MCP server definition (tools and the `psdomain://concepts`
resource) is imported from `ps_service.mcp_interface.mcp_server` directly; its Streamable HTTP
transport lives in `ps_service.mcp_interface.http_transport`.
"""

from __future__ import annotations

import ps_service.mcp_interface.audit_actions  # noqa: F401  # pyright: ignore[reportUnusedImport] -- registers `query.cypher` on package import
from ps_service.mcp_interface.errors import (
    McpGraphUnavailableError,
    McpResourceUnavailableError,
)
from ps_service.mcp_interface.mcp_server import handle_mcp_tool_call

__all__ = [
    "McpGraphUnavailableError",
    "McpResourceUnavailableError",
    "handle_mcp_tool_call",
]
