"""Cypher and domain-concepts tools of the PS MCP server, and the injectable cypher entry point.

Declares the read-only `run-cypher` and `get-domain-concepts` tools and the `psdomain://concepts`
resource; `register_cypher_tools` registers them on a server built by
`mcp_server.build_server`. Read-only graph queries run in-process through
`ps_service.query_engine.execute_cypher_query`; the write-clause guard and query execution live in
Query Engine and are never duplicated here.
`handle_mcp_tool_call` is the transport-free, dependency-injectable entry point the `run-cypher`
tool and the tests share; `mcp_server.handle_mcp_tool_call` re-exports it. This module imports only
`tool_core` and `tool_gates` (never another tool group).

Business Rules:
    - Every `run-cypher` call writes one `query.cypher` audit row and nothing to the graph.
    - The domain-concepts document is read lazily from the installed package, so importing this
      module performs no I/O.
"""

from __future__ import annotations

import dataclasses
import functools
from importlib import resources
from typing import TYPE_CHECKING, Literal

from ps_service.audit.emit import AuditTarget, record_follow_up_row
from ps_service.audit.errors import AuditActorUnresolvedError
from ps_service.config import ServiceConfigurationError, load_config
from ps_service.domain_schema import DOMAIN_SCHEMA, render_slim_schema
from ps_service.logging import bind_run_context, emit_log_entry
from ps_service.mcp_interface.audit_actions import (
    QUERY_CYPHER_ACTION,
    QUERY_CYPHER_RESOURCE_ID,
    QUERY_RESOURCE_TYPE,
    QueryCypherReasonCode,
)
from ps_service.mcp_interface.errors import McpGraphUnavailableError, McpResourceUnavailableError
from ps_service.mcp_interface.tool_core import COMPONENT as _COMPONENT
from ps_service.mcp_interface.tool_core import (
    GRAPH_UNAVAILABLE_MESSAGE as _GRAPH_UNAVAILABLE_MESSAGE,
)
from ps_service.mcp_interface.tool_core import PLAIN_READ_GATE as _PLAIN_READ_GATE
from ps_service.mcp_interface.tool_core import audit_context as _audit_context
from ps_service.mcp_interface.tool_core import declared_tool as _gated_tool
from ps_service.mcp_interface.tool_core import resolve_authz_actor as _resolve_authz_actor
from ps_service.mcp_interface.tool_core import resolve_graph as _resolve_graph
from ps_service.mcp_interface.tool_gates import GateEnvironment, register_declared_tools
from ps_service.query_engine import (
    GraphUnseededError,
    QueryEngineExecutionError,
    QueryResult,
    WriteClauseRejectedError,
    execute_cypher_query,
)

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

    from mcp.server import MCPServer

    from ps_service.config import ServiceConfig
    from ps_service.logging.emitter import LogEmitter
    from ps_service.mcp_interface.tool_gates import ToolGate
    from ps_service.query_engine.falkordb_client import GraphHandle


_ACTION = "handle_mcp_tool_call"
_DOMAIN_CONCEPTS_URI = "psdomain://concepts"
_DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL = "the ps-domain-concepts resource is currently unavailable"


@functools.cache
def _domain_concepts_path() -> Traversable:
    """Locate the packaged `ps-domain-concepts.md` resource file.

    Resolved with `importlib.resources` against the installed package, so it works from a wheel
    install as well as from a checkout. Lazy and cached: nothing is touched at import time.

    Returns:
        A `Traversable` for the file inside `ps_service.mcp_interface`; its existence is not
        checked.
    """
    return resources.files("ps_service.mcp_interface").joinpath("ps-domain-concepts.md")


@dataclasses.dataclass(frozen=True, slots=True)
class CypherCallOutcome:
    """The result of one in-process `run-cypher` run plus the facts its audit row records.

    Attributes:
        response: What the tool returns to the caller: the result dict, or an `error: ...` string.
        row_count: Rows returned, or `None` when the query did not run to completion.
        truncated: Whether the row cap cut the result, or `None` when it did not complete.
        reason_code: Why the call did not complete (`write_clause`, `graph_unseeded` or
            `query_failed`), or `None` when it succeeded. Never exception or query text.
    """

    response: dict[str, object] | str
    row_count: int | None
    truncated: bool | None
    reason_code: QueryCypherReasonCode | None


def run_cypher_for_tool(
    query: str,
    *,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
    timeout_ms: int,
    row_cap: int,
) -> CypherCallOutcome:
    """Run one read-only Cypher `query` in-process and classify how the call ended.

    The shared core of `handle_mcp_tool_call` and the `run-cypher` tool: the tool needs the outcome
    class for its audit row, while `handle_mcp_tool_call` returns only the response. This function
    writes no audit row and neither inspects nor rewrites `query`; Query Engine owns the
    write-clause guard.

    Args:
        query: Cypher text to execute; it must be read-only.
        graph: Handle of the graph to query.
        emitter: Log emitter forwarded to Query Engine; `None` leaves the choice to Query Engine.
        timeout_ms: Server-side execution bound in milliseconds, forwarded unchanged.
        row_cap: Maximum number of rows returned, forwarded unchanged.

    Returns:
        A `CypherCallOutcome`. On success `response` is `{"columns", "rows", "row_count",
        "truncated"}`; otherwise it is the `error: <message>` string, verbatim from Query Engine.

    Raises:
        LoggingLifecycleError: No log emitter is available (none given and no process default).

    Side Effects:
        Binds a run context for the call's duration. Query Engine emits one log entry carrying the
        run id. Nothing is written to the graph or the audit trail.
    """
    with bind_run_context():
        try:
            result: QueryResult = execute_cypher_query(
                query,
                graph=graph,
                emitter=emitter,
                timeout_ms=timeout_ms,
                row_cap=row_cap,
            )
        except WriteClauseRejectedError as exc:
            return CypherCallOutcome(f"error: {exc}", None, None, "write_clause")
        except GraphUnseededError as exc:
            return CypherCallOutcome(f"error: {exc}", None, None, "graph_unseeded")
        except QueryEngineExecutionError as exc:
            return CypherCallOutcome(f"error: {exc}", None, None, "query_failed")
    response: dict[str, object] = {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }
    return CypherCallOutcome(response, result.row_count, result.truncated, None)


def handle_mcp_tool_call(
    query: str,
    *,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
    timeout_ms: int,
    row_cap: int,
) -> dict[str, object] | str:
    """Run one read-only Cypher `query` through Query Engine in-process, as `run-cypher` does.

    Binds a fresh run id for the call, then executes the query with the given bounds. Query Engine
    owns the write-clause guard, so this layer neither inspects nor rewrites `query`.

    Args:
        query: Cypher text to execute; it must be read-only.
        graph: Handle of the graph to query.
        emitter: Log emitter forwarded to Query Engine for its log entry; `None` leaves the choice
            to Query Engine.
        timeout_ms: Server-side execution bound in milliseconds, forwarded unchanged.
        row_cap: Maximum number of rows returned, forwarded unchanged; `truncated` reports whether
            it cut the result.

    Returns:
        `{"columns", "rows", "row_count", "truncated"}` on success (`rows` may be empty), or an
        `error: <message>` string, verbatim from Query Engine, for a rejected write clause, an
        unseeded graph or a failed query.

    Raises:
        LoggingLifecycleError: No log emitter is available (none given and no process default).

    Side Effects:
        Binds a run context for the call's duration. Query Engine emits one log entry carrying the
        run id. Nothing is written to the graph.
    """
    return run_cypher_for_tool(
        query,
        graph=graph,
        emitter=emitter,
        timeout_ms=timeout_ms,
        row_cap=row_cap,
    ).response


def _record_cypher_call(
    environment: GateEnvironment,
    config: ServiceConfig,
    actor: tuple[str, str] | None,
    outcome: CypherCallOutcome,
) -> None:
    """Write the one `query.cypher` audit row of a `run-cypher` tool call; BEST-EFFORT.

    The row records the verified caller (or the local-test-bypass sentinel), the outcome class and
    the enumerated facts of `outcome`; never the query text or an error text. When no audit actor
    can be resolved (no verified caller and the bypass is off) no row is written and one failed log
    entry names the error class. A failed audit write is logged by `record_follow_up_row` and never
    changes the tool's result.

    Args:
        environment: Supplies the audit-store factory.
        config: Service configuration; selects the audit database and the bypass state.
        actor: Verified `(sub, iss)` of the caller, or `None` when none is bound.
        outcome: How the call ended.

    Side Effects:
        Inserts one `audit_events` row (`applied` when `reason_code` is `None`, `rejected` for a
        write clause, `failed` otherwise).
    """
    audit = _audit_context(config, actor, environment=environment)
    if isinstance(audit, str):
        emit_log_entry(
            component=_COMPONENT,
            action="cypher_audit_skipped",
            outcome="failed",
            extra={"reason": AuditActorUnresolvedError.__name__},
        )
        return
    reason_code = outcome.reason_code
    details: dict[str, object] = (
        {"row_count": outcome.row_count, "truncated": outcome.truncated}
        if reason_code is None
        else {"reason_code": reason_code}
    )
    audit_outcome: Literal["applied", "rejected", "failed"] = "failed"
    if reason_code is None:
        audit_outcome = "applied"
    elif reason_code == "write_clause":
        audit_outcome = "rejected"
    record_follow_up_row(
        audit,
        AuditTarget(QUERY_CYPHER_ACTION, QUERY_RESOURCE_TYPE, QUERY_CYPHER_RESOURCE_ID),
        component=_COMPONENT,
        outcome=audit_outcome,
        details=details,
    )


@_gated_tool("run-cypher", _PLAIN_READ_GATE)
def cypher(  # noqa: D417  # `environment` is injected by the server and hidden from clients
    environment: GateEnvironment, query: str
) -> dict[str, object] | str:
    """Run a read-only, MATCH/RETURN-shaped Cypher query against the policy_system graph.

    Call `get-domain-concepts` first and ground every query in the labels, properties and edges it
    lists.
    The query runs with the configured timeout and row cap.
    Policy, Standard and Control content is visible in every status, `draft` included, so filter
    on `status` when only approved content is wanted.

    Args:
        query: Cypher text. It must not contain a write clause (CREATE, MERGE, DELETE, SET, REMOVE,
            DROP, FOREACH); such a query is rejected before execution.

    Returns:
        A dict with `columns`, `rows`, `row_count` and `truncated` (`true` when more rows matched
        than
        the row cap returned). A query that legitimately matches nothing returns the normal empty
        shape.

    Errors (returned as a string beginning `error: `, never raised):
        - The query contains a write clause.
        - The graph has no seeded content at all yet.
        - The graph database rejects or fails to execute the query.
        - The graph database cannot be reached.

    Side Effects:
        One read-only graph query; nothing is written to the graph. The call is logged, and one
        `query.cypher` audit row records the outcome (applied, rejected or failed) with the row
        count, the truncation flag and an enumerated reason code, never the query text. The audit
        write is best-effort: a failure there is logged and does not change the result. No row is
        written when the service configuration cannot be loaded.
    """
    try:
        config = load_config()
    except ServiceConfigurationError:
        emit_log_entry(component=_COMPONENT, action=_ACTION, outcome="unavailable")
        return _GRAPH_UNAVAILABLE_MESSAGE
    actor = _resolve_authz_actor(config)
    try:
        graph = _resolve_graph(config, environment=environment)
    except McpGraphUnavailableError:
        emit_log_entry(component=_COMPONENT, action=_ACTION, outcome="unavailable")
        _record_cypher_call(
            environment,
            config,
            actor,
            CypherCallOutcome(_GRAPH_UNAVAILABLE_MESSAGE, None, None, "graph_unavailable"),
        )
        return _GRAPH_UNAVAILABLE_MESSAGE
    outcome = run_cypher_for_tool(
        query,
        graph=graph,
        timeout_ms=config.query_timeout_ms,
        row_cap=config.query_row_cap,
    )
    _record_cypher_call(environment, config, actor, outcome)
    return outcome.response


@_gated_tool("get-domain-concepts", _PLAIN_READ_GATE)
def domain_concepts() -> str:
    """Return the PS compliance-graph schema: node labels, properties and edges.

    Call this first and ground every Cypher query in it. Takes no parameters.

    Returns:
        Text listing each node label with its properties (type, enum values, required flag) and each
        edge with direction, cardinality and edge-property names. Prose explanations and worked
        examples
        are not included; read the `psdomain://concepts` resource for those.

    Errors:
        None; the text is built from the schema compiled into the service.

    Side Effects:
        None.
    """
    return render_slim_schema(DOMAIN_SCHEMA)


def _load_domain_concepts() -> str:
    """Read the packaged ps-domain-concepts.md text.

    Takes no parameters, so no client-supplied input reaches the file read.

    Returns:
        The full UTF-8 text of the packaged file.

    Raises:
        McpResourceUnavailableError: The file cannot be read; the message is a fixed, path-free
        detail.

    Side Effects:
        Reads one file from the installed package.
    """
    try:
        return _domain_concepts_path().read_text(encoding="utf-8")
    except OSError as exc:
        raise McpResourceUnavailableError(_DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL) from exc


def read_domain_concepts() -> str:
    """Return the full ps-domain-concepts.md text.

    This is the canonical PS compliance-graph vocabulary and schema, served verbatim.

    Takes no parameters.

    Returns:
        The file text verbatim. If the file is unavailable, a string beginning `error: ` followed by
        a
        fixed detail that carries no path and no cause.

    Errors:
        None raised; an unreadable file is reported through the returned string.

    Side Effects:
        Reads one file from the installed package.
    """
    try:
        return _load_domain_concepts()
    except McpResourceUnavailableError:
        return f"error: {_DOMAIN_CONCEPTS_UNAVAILABLE_DETAIL}"


def register_cypher_tools(
    server: MCPServer, environment: GateEnvironment, gates: dict[str, ToolGate]
) -> None:
    """Register the `run-cypher` and `get-domain-concepts` tools and the domain-concepts resource.

    Args:
        server: The server the tools and the resource are registered on.
        environment: The server-side collaborators the tools use.
        gates: The registry that records this server's gate declarations.

    Raises:
        ValueError: A tool name is already declared in `gates`.
    """
    register_declared_tools(server, environment, gates, (cypher, domain_concepts))
    server.resource(
        _DOMAIN_CONCEPTS_URI,
        name="ps-domain-concepts",
        title="PS domain concepts",
        description="The canonical PS compliance-graph vocabulary and schema, served verbatim.",
        mime_type="text/markdown",
    )(read_domain_concepts)
