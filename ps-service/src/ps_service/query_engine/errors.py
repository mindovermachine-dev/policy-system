"""Domain-specific exception types for `ps_service.query_engine`.

Mirrors `ps_service.company_merge.errors`'s/`ps_service.domain_mapper.
errors`'s shape (PLAN_REVIEWED.md §2.2): one exception type per distinct
failure boundary this component owns, never a generic `Exception`/
`ValueError` (L1 Error Handling, L2 Error Handling).
"""

from __future__ import annotations


class WriteClauseRejectedError(Exception):
    """Raised by `execute_cypher_query` when the query contains a write
    clause (CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH, case-insensitive)
    -- AC-002. Raised BEFORE `graph.query` is ever called.

    The message is `cypher_query._WRITE_CLAUSE_REJECTION_MESSAGE` verbatim
    -- no added prefix/wrapping text.
    """


class QueryEngineExecutionError(Exception):
    """Raised by `execute_cypher_query` when `graph.query(...)` itself
    raises. The message is the original exception's `str(exc)` verbatim
    (CA doc: "FalkorDB errors surfaced verbatim as error: <exception
    message>") -- no added prefix/wrapping text beyond what the original
    exception already said. Chained via `raise ... from exc`.
    """
