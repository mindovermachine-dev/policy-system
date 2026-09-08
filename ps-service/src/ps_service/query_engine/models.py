"""Core types for `ps_service.query_engine` -- `execute_cypher_query`'s return shape.

Per PLAN_REVIEWED.md §2.1: a single generic envelope, not per-entity
Pydantic models, matching the shape the existing `tools/graph-query/
mcp_server.py` prototype already returns.

Issue #38, Slice 3 (PLAN.md §4 S3, D1): `QueryResult` gains `truncated: bool`,
required with no default so every construction site must state it explicitly
(mirrors this dataclass's existing no-defaults convention for
`columns`/`rows`/`row_count`) -- `True` when `execute_cypher_query`'s
post-hoc Python-side row-cap slice actually shortened the result, `False`
otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueryResult:
    """ExecuteCypherQuery's success envelope.

    L2's mandated shape (`QueryResult(columns, rows, row_count)`), matching
    the existing `tools/graph-query/mcp_server.py` prototype's return shape,
    extended with `truncated` (issue #38, AC-BI-006).
    """

    columns: list[str]
    rows: list[list[object]]
    row_count: int
    truncated: bool
