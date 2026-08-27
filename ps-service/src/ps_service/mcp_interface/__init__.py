"""ps_service.mcp_interface -- package front door.

Re-exports the component's sanitised boundary exception types and the
injectable `HandleMcpToolCall` core. The `mcp_server` stdio wiring
(`cypher` tool, `psdomain://concepts` resource, `main()`) is imported
from `ps_service.mcp_interface.mcp_server` directly.
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
