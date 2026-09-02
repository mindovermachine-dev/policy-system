#!/usr/bin/env python3
"""MCP stdio server: in-process read-only Cypher access to the policy_system graph.

Also serves the ps-domain-concepts resource, for clients (e.g. Claude
Desktop) with no shell. Calls ps_service.query_engine.execute_cypher_query
IN-PROCESS. The write-clause guard and all execution live in Query Engine
and are never duplicated here. No network transport, no auth, no query
timeout / result-size cap (issues #38 / #39).
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server import MCPServer

from ps_service.config import LOCAL_TEST_PRINCIPAL_ID, ServiceConfigurationError, load_config
from ps_service.logging import bind_run_context, configure, emit_log_entry
from ps_service.mcp_interface.errors import (
    McpGraphUnavailableError,
    McpResourceUnavailableError,
)
from ps_service.query_engine import (
    QueryEngineExecutionError,
    QueryResult,
    WriteClauseRejectedError,
    execute_cypher_query,
)
from ps_service.query_engine.falkordb_client import (
    GraphHandle,
    connect_from_config,
    select_graph,
)

if TYPE_CHECKING:
    from ps_service.config import ServiceConfig
    from ps_service.logging.emitter import LogEmitter

_COMPONENT = "mcp_interface"
_ACTION = "handle_mcp_tool_call"
_DEFAULT_GRAPH_NAME = "policy_system"
_DOMAIN_CONCEPTS_URI = "psdomain://concepts"
_GRAPH_UNAVAILABLE_DETAIL = "the policy graph database is not reachable"
_GRAPH_UNAVAILABLE_MESSAGE = f"error: {_GRAPH_UNAVAILABLE_DETAIL}"


@functools.cache
def _domain_concepts_path() -> Path:
    """Absolute path to docs/artifacts/ps-domain-concepts.md.

    Resolved from the repo checkout by a fixed parent count (mirrors how
    CYPHER_CLI was computed with parents[1] today). Lazy + cached: never
    touched at import, so the module imports outside a checkout / in a
    wheel. Wheel-packaging docs/ is part of the same remote-deployment
    migration already flagged for the transport.
    """
    return Path(__file__).resolve().parents[4] / "docs" / "artifacts" / "ps-domain-concepts.md"


def _graph_name() -> str:
    """The single company-graph name.

    Reads PS_FALKORDB_GRAPH directly (config.py deliberately has no
    falkordb_graph field -- see PLAN_REVIEWED §2 Q4). Rejects an
    explicitly-empty value, mirroring config._parse_falkordb_host.
    """
    name = os.environ.get("PS_FALKORDB_GRAPH", _DEFAULT_GRAPH_NAME)
    if not name.strip():
        raise McpGraphUnavailableError(_GRAPH_UNAVAILABLE_DETAIL)
    return name


server = MCPServer(
    name="policy-system-graph",
    instructions=(
        "Read-only Cypher access to the policy_system compliance graph. "
        "Ground every query in the ps-domain-concepts resource's actual node labels, "
        "properties, and edge directions -- never invent one. "
        "Write clauses are rejected before execution and returned as an 'error:' line."
    ),
)


def handle_mcp_tool_call(
    query: str,
    *,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
    principal: str | None = None,
) -> dict[str, object] | str:
    """HandleMcpToolCall: run `query` through Query Engine in-process.

    Binds a fresh run_id, then returns `{columns, rows, row_count}` on
    success or an `error: <message>` string verbatim on a rejected or
    failed query.

    `principal` is an opaque caller identity string (issue #67), threaded
    straight through to `execute_cypher_query` so it lands on the
    `query_engine` log entry; omitted entirely when `None` (the default),
    matching Slice 3's silent-by-default behavior end to end. This layer
    never decides who the principal is -- see Slice 5 for where it's set.
    """
    with bind_run_context():
        try:
            result: QueryResult = execute_cypher_query(
                query, graph=graph, emitter=emitter, principal=principal
            )
        except (WriteClauseRejectedError, QueryEngineExecutionError) as exc:
            return f"error: {exc}"
    return {"columns": result.columns, "rows": result.rows, "row_count": result.row_count}


def _resolve_graph(config: ServiceConfig) -> GraphHandle:
    """Acquire a GraphHandle for one tool call, given an already-resolved config.

    ANY failure here -- DB unreachable/refused, driver I/O in the eager
    FalkorDB constructor or in select_graph -- is sanitised to a fixed
    generic McpGraphUnavailableError. Host, port, driver, and env-var text
    must not cross the MCP boundary (L2 MCP Interface Patterns; PLAN_REVIEWED
    §2 Q6 / F-01 / F-17). `config` is resolved by the caller (`cypher()`), not
    here, so a `ServiceConfigurationError` from `load_config()` is sanitised
    by the caller's own try/except rather than this function's.
    """
    try:
        return select_graph(connect_from_config(config), _graph_name())
    except McpGraphUnavailableError:
        raise
    # broad by design: every failure here is sanitised to a fixed message
    # and chained, never re-raised raw
    except Exception as exc:
        raise McpGraphUnavailableError(_GRAPH_UNAVAILABLE_DETAIL) from exc


@server.tool()
def cypher(query: str) -> dict[str, object] | str:
    """Run a read-only, MATCH/RETURN-shaped Cypher query against the policy_system graph.

    On success returns an object with `columns`, `rows`, and `row_count`. Returns a
    string beginning `error: ` when the query contains a write clause
    (CREATE, MERGE, DELETE, SET, REMOVE, DROP, FOREACH -- rejected before execution),
    when FalkorDB rejects the query, or when the graph database cannot be reached.
    """
    try:
        config = load_config()
        graph = _resolve_graph(config)
    except McpGraphUnavailableError, ServiceConfigurationError:
        emit_log_entry(component=_COMPONENT, action=_ACTION, outcome="unavailable")
        return _GRAPH_UNAVAILABLE_MESSAGE
    principal = LOCAL_TEST_PRINCIPAL_ID if config.is_local_test_bypass_active else None
    return handle_mcp_tool_call(query, graph=graph, principal=principal)


@server.resource(
    _DOMAIN_CONCEPTS_URI,
    name="ps-domain-concepts",
    title="PS domain concepts",
    description="The canonical PS compliance-graph vocabulary and schema, served verbatim.",
    mime_type="text/markdown",
)
def read_domain_concepts() -> str:
    """GetDomainConcepts: return the full ps-domain-concepts.md text.

    Takes no parameters -- no client-supplied input reaches the read
    (AC-012). Resolves from a repo checkout only; raises a
    resource-unavailable error if the file cannot be read.
    """
    try:
        return _domain_concepts_path().read_text(encoding="utf-8")
    except OSError as exc:
        raise McpResourceUnavailableError(
            "the ps-domain-concepts resource is currently unavailable"
        ) from exc


def main() -> None:
    """Install the process-wide default LogEmitter, then serve MCP over stdio."""
    configure()
    server.run()


if __name__ == "__main__":
    main()
