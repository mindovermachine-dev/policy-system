"""Core types for `ps_service.query_engine`: the return shape of `execute_cypher_query`.

A single generic envelope rather than per-entity models, because the queries are ad hoc.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Success envelope of `execute_cypher_query`.

    Immutable. Every field is required so each construction site states the truncation flag.

    Attributes:
        columns: Column names of the result, in `RETURN` order; empty when the query returned
            no header.
        rows: Result rows, each a list of JSON-serializable cell values aligned with `columns`;
            at most the caller's `row_cap` rows. Empty for a seeded graph with no match.
        row_count: Number of rows in `rows` (after truncation), not the number the database
            produced.
        truncated: `True` when the row cap shortened the result, so more rows existed;
            `False` otherwise.
    """

    columns: list[str]
    rows: list[list[object]]
    row_count: int
    truncated: bool
