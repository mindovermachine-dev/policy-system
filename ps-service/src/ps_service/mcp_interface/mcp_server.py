#!/usr/bin/env python3
"""MCP server front door: the one place that imports every tool-group module.

`server`, the single `MCPServer` instance, is defined in `tool_core` together with the gates and the
plumbing every tool shares. Each tool group registers its tools on `server` from its own module
as an import side effect: `ingestion_tools`, `near_miss_tools`, `catalog_tools`, `cypher_tools`,
`access_audit_tools`, `policy_lifecycle_tools` and `graph_cleanup_tools`. This module imports them
all, so importing `ps_service.mcp_interface` registers every tool and the `psdomain://concepts`
resource. It defines no tool itself; it exports `server` and `handle_mcp_tool_call`.

Business Rules:
    - The group modules are imported in the order that `tools/list` presents their tools in.
      Registration order is import order, so the imports sit in an isort-off fence: alphabetical
      sorting would reorder the tool list that clients see.
    - This module binds no transport and no authentication. The Streamable HTTP transport in the
      sibling `http_transport` module is the only transport and arms authentication on `server` from
      outside this file, so importing this module performs no I/O and no fail-closed check.
"""

from __future__ import annotations

from typing import Final

# isort: off
# Registration order is import order: keep ingestion first and graph cleanup last (see docstring).
from ps_service.mcp_interface import ingestion_tools
from ps_service.mcp_interface import near_miss_tools
from ps_service.mcp_interface import catalog_tools
from ps_service.mcp_interface import cypher_tools
from ps_service.mcp_interface import access_audit_tools
from ps_service.mcp_interface import policy_lifecycle_tools
from ps_service.mcp_interface import graph_cleanup_tools

# isort: on
from ps_service.mcp_interface.cypher_tools import handle_mcp_tool_call
from ps_service.mcp_interface.tool_core import server

__all__ = ["TOOL_GROUP_MODULES", "handle_mcp_tool_call", "server"]

# The runtime use of every group import (so none is an unused import) and the list the structure
# guard compares with the `*_tools.py` files on disk, so an unregistered group module fails a test.
TOOL_GROUP_MODULES: Final = (
    ingestion_tools,
    near_miss_tools,
    catalog_tools,
    cypher_tools,
    access_audit_tools,
    policy_lifecycle_tools,
    graph_cleanup_tools,
)
