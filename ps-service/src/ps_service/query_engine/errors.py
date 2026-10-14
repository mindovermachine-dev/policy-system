"""Domain-specific exception types for `ps_service.query_engine`.

One exception type per distinct failure boundary this component owns, never a generic
`Exception`/`ValueError`. All are raised by `execute_cypher_query`.
"""

from __future__ import annotations


class WriteClauseRejectedError(Exception):
    """The query contains a write clause, so it was not executed.

    The clause set is CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH (case-insensitive). Raised
    before any graph call is made. The message is a fixed sentence naming the clause set,
    with no added prefix or wrapping text. The caller should send a read-only query.
    """


class QueryEngineExecutionError(Exception):
    """The graph database raised while the seed check or the caller's query ran.

    The message is the original exception's `str(exc)` verbatim, with no added prefix or
    wrapping text, so the caller can surface the database's own explanation (a syntax error,
    a timeout). The original exception is chained via `raise ... from exc`.
    """


class GraphUnseededError(Exception):
    """The target graph has no content at all, so the caller's query was not run.

    `execute_cypher_query` issues one cheap `count(n)` read before the caller's own query;
    a count of zero raises this error instead, so a totally unseeded graph is distinguishable
    from a seeded graph's legitimate zero-row answer (which still returns a normal
    `QueryResult`). The message is a fixed, sanitized sentence carrying no host, port or path
    detail. The caller should seed (ingest into) the graph before querying.
    """
