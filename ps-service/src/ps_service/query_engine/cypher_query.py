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
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, cast

from ps_service.logging.facade import emit_log_entry
from ps_service.query_engine.errors import (
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


def is_write_clause(query: str) -> bool:
    """Return True if `query` contains a write clause.

    Single source of truth for the AC-002 check -- the boundary every
    caller goes through, including `mcp_interface`'s in-process
    `handle_mcp_tool_call`. One call site (`execute_cypher_query`);
    exported, not duplicated as a second regex.
    """
    return bool(_WRITE_CLAUSE.search(query))


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

    Any exception from `graph.query` itself is caught and re-raised as
    `QueryEngineExecutionError`, preserving the original message verbatim.

    With a `run_id` bound via `bind_run_context`, emits one structured log
    entry per call, `outcome="succeeded"`/`"rejected"`/`"failed"`, carrying
    the bound `run_id` (AC-004). Never logs the raw query text -- see the
    module docstring.

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
