"""`ps-test-plugin-mcp`: a local stdio MCP server exposing a single `HelloWorld` tool.

A console-script entry point (`pyproject.toml`'s `[project.scripts]`), never run
manually -- an MCP host (a Claude plugin's `.mcp.json` declaring a `type: "stdio"`
server) spawns it and speaks MCP JSON-RPC to it over stdin/stdout, the same shape
as `ps-cli`'s `ps-cli-mcp-bridge` (see `ps_cli.mcp_bridge`).

Exists purely to validate the plugin/MCP packaging path (Claude Desktop marketplace
install, `.mcp.json` wiring, tool discovery) independent of the real `policy-system`
plugin's remote-bridge and auth complexity -- a disposable reference, not a product
surface.
"""

from __future__ import annotations

import random

from mcp.server import MCPServer

_CITIES = (
    "Amsterdam",
    "Copenhagen",
    "Lisbon",
    "Nairobi",
    "Osaka",
    "Reykjavik",
    "Santiago",
    "Toronto",
    "Wellington",
    "Zurich",
)

server = MCPServer(
    name="policy-system-test",
    instructions="Diagnostic plugin exposing a single HelloWorld tool; no real functionality.",
)


@server.tool(name="HelloWorld")
def hello_world() -> str:
    """Return a greeting from a randomly chosen city, e.g. `"Hello from Lisbon!"`."""
    return f"Hello from {random.choice(_CITIES)}!"  # noqa: S311 -- cosmetic city pick, not security-sensitive


def main() -> None:
    """Run the stdio MCP server until stdin closes."""
    server.run()


if __name__ == "__main__":
    main()
