#!/usr/bin/env python3
"""MCP server front door: the one place that imports every tool-group module.

Each tool group lives in its own `*_tools` module and registers its tools through a
`register_<group>_tools(server, environment, gates)` function listed in `TOOL_GROUP_MODULES`.
`build_server(environment)` creates a server and calls those functions on it, so a server is built
over any `GateEnvironment`. This module builds the production server once, over
`build_production_environment()`, and exports it as `server` with its gate declarations as
`TOOL_GATES`. The groups are `ingestion_tools`, `near_miss_tools`, `catalog_tools`,
`cypher_tools`, `access_audit_tools`, `policy_draft_tools`, `policy_transition_tools` and
`graph_cleanup_tools`; importing this module imports them all. It defines no tool itself; it exports
`server`, `TOOL_GATES`, `build_server` and `handle_mcp_tool_call`.

Business Rules:
    - `TOOL_GROUP_MODULES` is in the order that `tools/list` presents the groups' tools in.
    - This module binds no transport and no authentication. The Streamable HTTP transport in the
      sibling `http_transport` module is the only transport and arms authentication on `server` from
      outside this file. Building the production server performs no I/O and no fail-closed check;
      the environment's factories connect when a tool calls them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from ps_service.mcp_interface import (
    access_audit_tools,
    catalog_tools,
    cypher_tools,
    graph_cleanup_tools,
    ingestion_tools,
    near_miss_tools,
    policy_draft_tools,
    policy_transition_tools,
)
from ps_service.mcp_interface.access_audit_tools import register_access_audit_tools
from ps_service.mcp_interface.catalog_tools import register_catalog_tools
from ps_service.mcp_interface.cypher_tools import handle_mcp_tool_call, register_cypher_tools
from ps_service.mcp_interface.graph_cleanup_tools import register_graph_cleanup_tools
from ps_service.mcp_interface.ingestion_tools import register_ingestion_tools
from ps_service.mcp_interface.near_miss_tools import register_near_miss_tools
from ps_service.mcp_interface.policy_draft_tools import register_policy_draft_tools
from ps_service.mcp_interface.policy_transition_tools import register_policy_transition_tools
from ps_service.mcp_interface.production_environment import build_production_environment
from ps_service.mcp_interface.tool_core import BuiltServer, create_server
from ps_service.mcp_interface.tool_gates import install_strict_arguments

if TYPE_CHECKING:
    from types import ModuleType

    from ps_service.mcp_interface.tool_gates import GateEnvironment, ToolGate, ToolGroupRegistrar

__all__ = [
    "TOOL_GATES",
    "TOOL_GROUP_MODULES",
    "BuiltServer",
    "build_server",
    "handle_mcp_tool_call",
    "server",
]

# Each group module with the function that registers its tools on a server, in the order
# `tools/list` presents the groups. The structure guard compares the modules with the
# `*_tools.py` files on disk, so an unregistered group module fails a test.
type ToolGroups = tuple[tuple[ModuleType, ToolGroupRegistrar | None], ...]

TOOL_GROUP_MODULES: Final[ToolGroups] = (
    (ingestion_tools, register_ingestion_tools),
    (near_miss_tools, register_near_miss_tools),
    (catalog_tools, register_catalog_tools),
    (cypher_tools, register_cypher_tools),
    (access_audit_tools, register_access_audit_tools),
    (policy_draft_tools, register_policy_draft_tools),
    (policy_transition_tools, register_policy_transition_tools),
    (graph_cleanup_tools, register_graph_cleanup_tools),
)


def build_server(
    environment: GateEnvironment, *, groups: ToolGroups = TOOL_GROUP_MODULES
) -> BuiltServer:
    """Build a new server with every registered tool group bound to `environment`.

    Each call creates its own server and its own gate registry, so any number of servers can
    coexist in one process.

    Args:
        environment: The server-side collaborators the tools use.
        groups: The tool groups to register, in `tools/list` order; defaults to every group.

    Returns:
        The server and the gate declarations registered on it.
    """
    built_server = create_server()
    gates: dict[str, ToolGate] = {}
    for _, register in groups:
        if register is not None:
            register(built_server, environment, gates)
    install_strict_arguments(built_server)
    return BuiltServer(server=built_server, gates=gates)


_production = build_server(build_production_environment())
server: Final = _production.server
TOOL_GATES: Final = _production.gates
