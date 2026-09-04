"""`ExecuteCypherQuery` -- the guard/execution core.

Batch 3 / Increments 3-5 scope (PLAN_REVIEWED.md §5): the write-clause guard
(`is_write_clause`, `_WRITE_CLAUSE_REJECTION_MESSAGE`) and `execute_cypher_query`'s
AC-001 success-path shape mapping and AC-002 rejection behavior, plus wrapping
any `graph.query` failure as `QueryEngineExecutionError`.

Batch 4 / Increment 6 (PLAN_REVIEWED.md §2.4, §4 AC-004): structured logging
is wired in via `_log`, called on every branch (rejected/failed/succeeded).
Mirrors `ps_service/llm_interface/completion.py`'s `route_completion` shape
(a single infra call, try/except, `outcome=`, `duration_ms` via
`time.perf_counter()`) -- deliberately deviating from that precedent's own
`extra={"model": ...}` by never logging the raw query text: an arbitrary
caller-supplied Cypher query can embed values that may carry PII (L1:
"never log secrets, tokens, or personally identifiable information").
`_log`'s `extra` therefore carries only `row_count`, and only on success.

Batch 8 (PLAN.md D11, AC-BI-013/AC-BI-014/AC-BI-015): `_is_graph_seeded`
issues one cheap, generic, label-agnostic `count(n)` read -- ordered after
the (cheap, no-I/O) write-clause guard above and before the caller's own
query is ever sent to FalkorDB -- so a totally unseeded graph raises
`GraphUnseededError` (a new `outcome="unseeded"` log branch) instead of
running the caller's query at all. A seeded graph's own legitimate
zero-row answer is unaffected: it still reaches the normal
`{columns, rows: [], row_count: 0}` shape via the existing success path
below.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, cast

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
# security boundary. Single-sourced here; not duplicated anywhere else.
_WRITE_CLAUSE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|FOREACH)\b", re.IGNORECASE)

_WRITE_CLAUSE_REJECTION_MESSAGE = (
    "this command is read-only -- query contains a write clause "
    "(CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH). Not executed."
)  # returned to the caller verbatim, prefixed with "error: "

# D11: one cheap, generic, label-agnostic read -- no label appears in this
# query at all, so L2 Query Safety's label-interpolation allow-list rule
# does not apply here.
_SEED_CHECK_QUERY = "MATCH (n) RETURN count(n) AS c LIMIT 1"

_GRAPH_UNSEEDED_DETAIL = "the policy graph has no seeded content yet"
# returned to the caller verbatim, prefixed with "error: " -- a fixed,
# sanitized string carrying no host/port/path detail (AC-BI-015), mirroring
# mcp_interface.mcp_server._GRAPH_UNAVAILABLE_DETAIL's convention.


def is_write_clause(query: str) -> bool:
    """Return True if `query` contains a write clause.

    Single source of truth for the AC-002 check -- the boundary every
    caller goes through, including `mcp_interface`'s in-process
    `handle_mcp_tool_call`. One call site (`execute_cypher_query`);
    exported, not duplicated as a second regex.
    """
    return bool(_WRITE_CLAUSE.search(query))


def _is_graph_seeded(graph: GraphHandle) -> bool:
    """Return True if `graph` has at least one node.

    Issues `_SEED_CHECK_QUERY` -- one cheap, generic, label-agnostic read --
    so `execute_cypher_query` can raise `GraphUnseededError` before the
    caller's own query is ever sent to FalkorDB (D11). An empty result set
    is treated as unseeded defensively; a real `count(n)` aggregate always
    returns exactly one row.
    """
    result = graph.query(_SEED_CHECK_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    if not rows:
        return False
    count = cast("int", rows[0][0])
    return count > 0


def execute_cypher_query(
    query: str,
    *,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
    principal: str | None = None,
) -> QueryResult:
    """ExecuteCypherQuery: execute a read-only Cypher query against `graph`.

    Rejects any query containing a write clause by raising
    `WriteClauseRejectedError` BEFORE `graph.query` is ever called (AC-002),
    via the shared `is_write_clause` helper.

    Otherwise, checks `_is_graph_seeded(graph)` -- one cheap `count(n)` read
    -- and raises `GraphUnseededError` if the graph has no content at all,
    BEFORE the caller's own query is ever sent (D11, AC-BI-013/AC-BI-014).
    This makes a totally unseeded graph distinguishable from a seeded
    graph's own legitimate zero-row answer, which still reaches the normal
    success shape below unaffected.

    Any exception from the seed check or from `graph.query` itself is
    caught and re-raised as `QueryEngineExecutionError`, preserving the
    original message verbatim.

    With a `run_id` bound via `bind_run_context`, emits one structured log
    entry per call, `outcome="succeeded"`/`"rejected"`/`"unseeded"`/
    `"failed"`, carrying the bound `run_id` (AC-004). Never logs the raw
    query text -- see the module docstring.

    `principal` is an opaque caller identity string (issue #67), attached to
    the log entry when given (on every branch, not only success) and omitted
    entirely when `None` (the default) -- groundwork for AC-BI-008; this
    layer never decides who the principal is, it only threads what it's
    given.
    """
    started = time.perf_counter()
    if is_write_clause(query):
        _log(outcome="rejected", started=started, emitter=emitter, principal=principal)
        raise WriteClauseRejectedError(_WRITE_CLAUSE_REJECTION_MESSAGE)

    try:
        seeded = _is_graph_seeded(graph)
    except Exception as exc:
        _log(outcome="failed", started=started, emitter=emitter, principal=principal)
        raise QueryEngineExecutionError(str(exc)) from exc

    if not seeded:
        _log(outcome="unseeded", started=started, emitter=emitter, principal=principal)
        raise GraphUnseededError(_GRAPH_UNSEEDED_DETAIL)

    try:
        result = graph.query(query)
    except Exception as exc:
        _log(outcome="failed", started=started, emitter=emitter, principal=principal)
        raise QueryEngineExecutionError(str(exc)) from exc

    columns = [cast("str", c[1]) for c in result.header] if result.header else []
    rows = [list(r) for r in cast("list[list[object]]", result.result_set)]
    _log(
        outcome="succeeded",
        started=started,
        emitter=emitter,
        row_count=len(rows),
        principal=principal,
    )
    return QueryResult(columns=columns, rows=rows, row_count=len(rows))


def _log(
    *,
    outcome: str,
    started: float,
    emitter: LogEmitter | None,
    row_count: int | None = None,
    principal: str | None = None,
) -> None:
    """Emit one `LogEntry` for a completed `execute_cypher_query` call.

    Never logs the query text -- `extra` carries only `row_count` (success
    only) and `principal` (issue #67, any branch), each added only when not
    `None` (S1/§0.5 PII-safety deviation from `route_completion`'s own
    `extra={"model": ...}` precedent). `run_id` is never passed explicitly,
    so `emit_log_entry` auto-bakes the currently bound run context -- the
    mechanism AC-004 relies on.
    """
    duration_ms = (time.perf_counter() - started) * 1000
    extra: dict[str, object] = {}
    if row_count is not None:
        extra["row_count"] = row_count
    if principal is not None:
        extra["principal"] = principal
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        outcome=outcome,
        duration_ms=duration_ms,
        extra=extra,
        emitter=emitter,
    )
