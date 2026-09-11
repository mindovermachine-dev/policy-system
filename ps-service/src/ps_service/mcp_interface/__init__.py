"""ps_service.mcp_interface -- package front door.

Re-exports the component's sanitised boundary exception types and the
injectable `HandleMcpToolCall` core. The `mcp_server` surface definition
(`cypher` tool, `psdomain://concepts` resource) is imported from
`ps_service.mcp_interface.mcp_server` directly; its Streamable HTTP
transport lives in `ps_service.mcp_interface.http_transport`.
"""

from __future__ import annotations

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
