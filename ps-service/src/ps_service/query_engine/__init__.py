"""ps_service.query_engine -- package front door.

Re-exports the public surface per PLAN_REVIEWED.md §2.5, now that
`cypher_query.py` (Batch 3) has landed alongside `models.py`/`errors.py`
(Batch 1).
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
