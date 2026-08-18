#!/usr/bin/env python3
"""MCP stdio server exposing read-only Cypher access to the policy_system
graph, for clients (e.g. Claude Desktop) that have no shell of their own.

Deliberately a thin wrapper around `ps.py cypher`, not a reimplementation:
every query is executed via subprocess through the exact same script the
policy-question skill's guardrails already document, so the write-clause
guard (see ps.py's _WRITE_CLAUSE) and connection logic live in exactly one
place. Do not duplicate that regex here.

Host/port/graph defaults can be overridden per call, or globally via
PS_FALKORDB_HOST / PS_FALKORDB_PORT / PS_FALKORDB_GRAPH env vars (useful
when FalkorDB is reachable at a non-default address, e.g. the devcontainer's
`falkordb` hostname instead of `localhost`).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server import MCPServer

PS_PY = Path(__file__).resolve().with_name("ps.py")

DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = os.environ.get("PS_FALKORDB_PORT", "6379")
DEFAULT_GRAPH = os.environ.get("PS_FALKORDB_GRAPH", "policy_system")

server = MCPServer(
    name="policy-system-graph",
    instructions=(
        "Read-only Cypher access to the policy_system compliance graph. "
        "Ground every query in docs/artifacts/ps-domain-concepts.md's actual "
        "node labels, properties, and edge directions -- never invent one. "
        "Write clauses (CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH) are "
        "rejected before execution by the underlying ps.py CLI."
    ),
)


@server.tool()
def cypher(query: str, host: str = "", port: str = "", graph: str = "") -> str:
    """Run a read-only MATCH/RETURN-shaped Cypher query against the graph.

    Returns JSON with `columns`, `rows`, and `row_count` on success, or an
    `error: ...` line (unexecuted) if the query contains a write clause or
    FalkorDB rejects it.
    """
    cmd = [
        sys.executable,
        str(PS_PY),
        "--host", host or DEFAULT_HOST,
        "--port", str(port or DEFAULT_PORT),
        "--graph", graph or DEFAULT_GRAPH,
        "--format", "json",
        "cypher",
        query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return result.stderr.strip() or "error: ps.py exited non-zero with no stderr output"
    return result.stdout


if __name__ == "__main__":
    server.run()
