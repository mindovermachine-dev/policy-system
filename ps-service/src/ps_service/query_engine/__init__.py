"""Query Engine: read-only Cypher execution against the compliance graph.

Re-exports the public surface: `execute_cypher_query` (guards, bounds and runs a read-only
query), its `QueryResult` envelope, and the three errors it raises
(`WriteClauseRejectedError`, `GraphUnseededError`, `QueryEngineExecutionError`).
"""

from __future__ import annotations

from ps_service.query_engine.cypher_query import execute_cypher_query
from ps_service.query_engine.errors import (
    GraphUnseededError,
    QueryEngineExecutionError,
    WriteClauseRejectedError,
)
from ps_service.query_engine.models import QueryResult

__all__ = [
    "GraphUnseededError",
    "QueryEngineExecutionError",
    "QueryResult",
    "WriteClauseRejectedError",
    "execute_cypher_query",
]
