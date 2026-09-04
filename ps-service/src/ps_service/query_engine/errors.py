"""Domain-specific exception types for `ps_service.query_engine`.

Mirrors `ps_service.company_merge.errors`'s/`ps_service.domain_mapper.
errors`'s shape (PLAN_REVIEWED.md §2.2): one exception type per distinct
failure boundary this component owns, never a generic `Exception`/
`ValueError` (L1 Error Handling, L2 Error Handling).
"""

from __future__ import annotations


class WriteClauseRejectedError(Exception):
    """Raised by `execute_cypher_query` when the query contains a write clause.

    The clause set is CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH
    (case-insensitive) -- AC-002; raised BEFORE `graph.query` is ever
    called. The message is `cypher_query._WRITE_CLAUSE_REJECTION_MESSAGE`
    verbatim -- no added prefix/wrapping text.
    """


class QueryEngineExecutionError(Exception):
    """Raised by `execute_cypher_query` when `graph.query(...)` itself raises.

    The message is the original exception's `str(exc)` verbatim (CA doc:
    "FalkorDB errors surfaced verbatim as error: <exception message>") --
    no added prefix/wrapping text beyond what the original exception
    already said. Chained via `raise ... from exc`.
    """


class GraphUnseededError(Exception):
    """Raised by `execute_cypher_query` when the target graph has no content at all.

    D11/AC-BI-013/AC-BI-014: `_is_graph_seeded` (`cypher_query.py`) issues
    one cheap `count(n)` read before the caller's own query is ever sent;
    a `count(n) = 0` result raises this error instead of running that
    query, so a totally unseeded graph is distinguishable from a seeded
    graph's legitimate zero-row answer (which still returns the normal
    `QueryResult` shape). The message is
    `cypher_query._GRAPH_UNSEEDED_DETAIL` verbatim -- a fixed, sanitized
    string carrying no host/port/path detail (AC-BI-015), mirroring
    `mcp_interface.mcp_server._GRAPH_UNAVAILABLE_DETAIL`'s convention.
    """
