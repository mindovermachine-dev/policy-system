"""`execute_cypher_query`: the guard and execution core of the Query Engine.

A query passes three gates before its result is returned: a textual write-clause guard
(`is_write_clause`) that rejects mutating Cypher before any graph call, a cheap `count(n)`
seed check that raises `GraphUnseededError` for a graph with no content (so an unseeded graph
is distinguishable from a seeded graph's legitimate zero-row answer), and the caller's own
query, run with a server-side timeout. Rows are then cut to the caller's row cap in Python,
after the database has returned them, never by a `LIMIT` clause or FalkorDB's instance-wide
`RESULTSET_SIZE` setting (which would affect other components sharing the instance);
`QueryResult.truncated` is the only signal that more rows existed.

Every call logs one structured entry (`rejected`, `unseeded`, `failed`, `timeout` or
`succeeded`) carrying the bound run id, the duration and, on success, the row count. The raw
query text is never logged: an arbitrary caller-supplied query can embed values that carry
personal data. FalkorDB has no dedicated timeout exception, so a failure whose message contains
"timed out" is logged as `timeout`; the exception raised to the caller is the same either way.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

from ps_service.logging.facade import emit_log_entry
from ps_service.query_engine.errors import (
    GraphUnseededError,
    QueryEngineExecutionError,
    WriteClauseRejectedError,
)
from ps_service.query_engine.models import QueryResult

if TYPE_CHECKING:
    from ps_service.logging.emitter import LogEmitter
    from ps_service.query_engine.falkordb_client import GraphHandle

_COMPONENT = "query_engine"
_ACTION = "execute_cypher_query"

# Cypher clauses that mutate the graph. Best-effort textual guard, not a
# security boundary (the read-only graph handle is). Single-sourced here.
_WRITE_CLAUSE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|FOREACH)\b", re.IGNORECASE)

_WRITE_CLAUSE_REJECTION_MESSAGE = (
    "this command is read-only -- query contains a write clause "
    "(CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH). Not executed."
)  # returned to the caller verbatim, prefixed with "error: "

# One cheap, generic, label-agnostic read: no label appears in this query at all, so the
# label-interpolation allow-list rule does not apply here.
_SEED_CHECK_QUERY = "MATCH (n) RETURN count(n) AS c LIMIT 1"

_GRAPH_UNSEEDED_DETAIL = "the policy graph has no seeded content yet"
# returned to the caller verbatim, prefixed with "error: " -- a fixed,
# sanitized string carrying no host/port/path detail.


def is_write_clause(query: str) -> bool:
    """Report whether `query` contains a Cypher write clause.

    The single textual write check, used by `execute_cypher_query` for every caller. It is a
    best-effort guard: it matches the keywords CREATE, MERGE, DELETE, SET, REMOVE, DROP and
    FOREACH case-insensitively as whole words anywhere in the text, so it can also reject a
    read query that merely contains such a word (for example in a string literal).

    Args:
        query: Cypher query text.

    Returns:
        `True` when a write keyword is present, otherwise `False`.
    """
    return bool(_WRITE_CLAUSE.search(query))


def _is_graph_seeded(graph: GraphHandle) -> bool:
    """Return True if `graph` has at least one node.

    Issues `_SEED_CHECK_QUERY` -- one cheap, generic, label-agnostic read --
    so `execute_cypher_query` can raise `GraphUnseededError` before the
    caller's own query is ever sent to FalkorDB. An empty result set
    is treated as unseeded defensively; a real `count(n)` aggregate always
    returns exactly one row. Any exception from `graph.query` propagates.
    """
    result = graph.query(_SEED_CHECK_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return False
    count = cast("int", rows[0][0])
    return count > 0


def _classify_execution_failure(exc: Exception) -> Literal["timeout", "failed"]:
    """Classify a `graph.query` failure as `"timeout"` or `"failed"` for logging only.

    FalkorDB gives no dedicated timeout exception type, so this is a
    best-effort message-substring check (`"timed out"`, case-insensitive)
    against the exception's own server-emitted text -- never the caller's
    query text. Only ever changes the log entry's `outcome`; the exception
    re-raised to the caller (`QueryEngineExecutionError(str(exc))`) is
    unaffected either way.
    """
    if "timed out" in str(exc).lower():
        return "timeout"
    return "failed"


@runtime_checkable
class _NodeLike(Protocol):
    """The attributes of a driver node (`falkordb.Node`) that the result conversion reads."""

    id: int
    labels: object
    properties: dict[str, object]


@runtime_checkable
class _EdgeLike(Protocol):
    """The attributes of a driver edge (`falkordb.Edge`) that the result conversion reads."""

    id: int
    relation: object
    properties: dict[str, object]
    src_node: object
    dest_node: object


def _to_jsonable(value: object) -> object:
    """Recursively convert a raw FalkorDB result value into a JSON-serializable shape.

    A freehand `RETURN r`/`RETURN r, rel` (whole node/relationship, not a projected property)
    is a normal shape for ad hoc Cypher; `graph.query` hands those back as raw driver
    node/edge objects, which a JSON tool-result encoder cannot serialize. A node becomes a dict
    with `id`, `labels` and `properties`; an edge a dict with `id`, `relation`, `properties`
    and its converted `src_node`/`dest_node`. Lists (for example a path's node/edge sequence)
    are converted element-wise; every other value (str, int, float, bool, None, plain dict)
    passes through unchanged.
    """
    if isinstance(value, _NodeLike):
        return {
            "id": value.id,
            "labels": value.labels,
            "properties": value.properties,
        }
    if isinstance(value, _EdgeLike):
        return {
            "id": value.id,
            "relation": value.relation,
            "properties": value.properties,
            "src_node": _to_jsonable(value.src_node),
            "dest_node": _to_jsonable(value.dest_node),
        }
    if isinstance(value, list):
        return [_to_jsonable(item) for item in cast("list[object]", value)]
    return value


def execute_cypher_query(
    query: str,
    *,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
    timeout_ms: int,
    row_cap: int,
    params: dict[str, object] | None = None,
) -> QueryResult:
    """Execute a read-only Cypher query against `graph` and return a bounded result.

    The query is rejected if it contains a write clause (see `is_write_clause`); a graph with
    no content at all is reported as unseeded instead of running the query, so it is
    distinguishable from a seeded graph's own legitimate zero-row answer. Rows are cut to
    `row_cap` in Python after the database has returned them, never via a `LIMIT` clause or
    FalkorDB's instance-wide `RESULTSET_SIZE` setting.

    Args:
        query: Cypher query text. Must not contain a write keyword.
        graph: Read-only graph handle to run against.
        emitter: Log emitter for the outcome entry; `None` uses the process default emitter.
        timeout_ms: Server-side time limit in milliseconds for the caller's query only (not
            the seed check). Required, so a caller cannot run an unbounded query by omission.
        row_cap: Maximum number of rows returned; rows beyond it are dropped.
        params: Values bound to the query's `$name` parameters, passed to the database's
            native `params` argument. Callers with client-supplied values must use this
            instead of interpolating into `query`. `None` binds nothing.

    Returns:
        A `QueryResult` with column names, at most `row_cap` JSON-serializable rows,
        `row_count` of the returned rows and `truncated` set when rows were dropped.

    Raises:
        WriteClauseRejectedError: `query` contains a write clause; nothing was sent to the
            graph.
        GraphUnseededError: the graph has no nodes; the caller's query was not run.
        QueryEngineExecutionError: the seed check or the query raised (including a server-side
            timeout); the message is the original exception's text and the cause is chained.
        LoggingLifecycleError: no `emitter` was given and no default emitter is configured.

    Side Effects:
        Sends one seed-check read and, if seeded, the caller's read to the graph, and emits one
        log entry with outcome `succeeded`, `rejected`, `unseeded`, `failed` or `timeout`,
        carrying the bound run id, duration and (on success) the row count. The query text and
        the caller's identity are never logged.
    """
    started = time.perf_counter()
    if is_write_clause(query):
        _log(outcome="rejected", started=started, emitter=emitter)
        raise WriteClauseRejectedError(_WRITE_CLAUSE_REJECTION_MESSAGE)

    try:
        seeded = _is_graph_seeded(graph)
    except Exception as exc:
        _log(outcome="failed", started=started, emitter=emitter)
        raise QueryEngineExecutionError(str(exc)) from exc

    if not seeded:
        _log(outcome="unseeded", started=started, emitter=emitter)
        raise GraphUnseededError(_GRAPH_UNSEEDED_DETAIL)

    try:
        result = graph.query(query, params=params, timeout=timeout_ms)
    except Exception as exc:
        outcome = _classify_execution_failure(exc)
        _log(outcome=outcome, started=started, emitter=emitter)
        raise QueryEngineExecutionError(str(exc)) from exc

    columns = [cast("str", c[1]) for c in result.header] if result.header else []
    rows = [
        [_to_jsonable(cell) for cell in row]
        for row in cast("list[list[object]]", result.result_set)
    ]
    full_row_count = len(rows)
    rows = rows[:row_cap]
    truncated = full_row_count > row_cap
    _log(
        outcome="succeeded",
        started=started,
        emitter=emitter,
        row_count=len(rows),
    )
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


def _log(
    *,
    outcome: str,
    started: float,
    emitter: LogEmitter | None,
    row_count: int | None = None,
) -> None:
    """Emit one log entry for a completed `execute_cypher_query` call.

    Never logs the query text or a caller identity: `extra` carries only `row_count`, added only
    when not `None` (success only). `run_id` is never passed explicitly, so
    `emit_log_entry` bakes in the currently bound run context.

    `outcome` is always one of a small closed vocabulary: `"succeeded"`, `"rejected"`,
    `"unseeded"`, `"failed"` or `"timeout"` -- never free text, and never the raw exception
    message that `_classify_execution_failure` inspected to pick `"timeout"`.
    """
    duration_ms = (time.perf_counter() - started) * 1000
    extra: dict[str, object] = {}
    if row_count is not None:
        extra["row_count"] = row_count
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        outcome=outcome,
        duration_ms=duration_ms,
        extra=extra,
        emitter=emitter,
    )
