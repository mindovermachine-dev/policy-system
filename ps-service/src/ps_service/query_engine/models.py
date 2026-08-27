"""ps_service.query_engine core types -- `execute_cypher_query`'s return
shape.

Per PLAN_REVIEWED.md §2.1: a single generic envelope, not per-entity
Pydantic models, matching the shape the existing `tools/graph-query/
mcp_server.py` prototype already returns.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueryResult:
    """ExecuteCypherQuery's success envelope -- L2's mandated shape
    (`QueryResult(columns, rows, row_count)`), matching the existing
    `tools/graph-query/mcp_server.py` prototype's return shape."""

    columns: list[str]
    rows: list[list[object]]
    row_count: int
