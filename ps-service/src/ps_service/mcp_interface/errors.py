"""Domain-specific exception types for `ps_service.mcp_interface`.

One exception type per distinct failure boundary this component owns, deliberately without a shared
base hierarchy: a graph-acquisition failure and a missing-resource-file failure are unrelated
boundaries.
"""

from __future__ import annotations


class McpGraphUnavailableError(Exception):
    """The FalkorDB-backed policy graph could not be acquired for an MCP tool call.

    Raised when the graph is unreachable or refuses the connection, when the driver fails with an
    I/O
    error while constructing its client, or when the `PS_FALKORDB_*` configuration is invalid. The
    message is deliberately generic: host, port, driver and environment-variable detail must not
    cross
    the MCP boundary. Tools catch it and return a fixed `error: ` string to the client.
    """


class McpResourceUnavailableError(Exception):
    """A backing file for an MCP resource (`ps-domain-concepts.md`) is missing or unreadable.

    Raised by the resource read function with a fixed, path-free message; the MCP SDK surfaces a
    clean
    resource-read error to the client, with no stack trace.
    """
