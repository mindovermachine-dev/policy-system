"""Domain-specific exception types for `ps_service.mcp_interface`.

One exception type per distinct failure boundary this component owns
(L2 "one exception type per distinct failure boundary") -- deliberately
NOT a shared base hierarchy: a graph-acquisition failure and a
missing-resource-file failure are unrelated boundaries.
"""

from __future__ import annotations


class McpGraphUnavailableError(Exception):
    """The FalkorDB-backed policy graph could not be acquired for an MCP tool call.

    Covers unreachable, refused, a driver I/O failure in the eager client
    constructor, or an invalid PS_FALKORDB_* configuration. The message is
    deliberately generic: host / port / driver / env-var detail must not
    cross the MCP boundary (L2 MCP Interface Patterns).
    """


class McpResourceUnavailableError(Exception):
    """A backing file for an MCP resource (ps-domain-concepts.md) is missing or unreadable.

    Raised by the resource read function; the mcp SDK surfaces a clean
    resource-read error to the client (no stack trace).
    """
