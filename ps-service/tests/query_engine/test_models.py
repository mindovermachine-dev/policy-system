"""Tests for ps_service.query_engine.models."""

from __future__ import annotations

import dataclasses

import pytest

from ps_service.query_engine.models import QueryResult


def _query_result() -> QueryResult:
    return QueryResult(columns=["n"], rows=[["obl_risk_a1b2c3"]], row_count=1)


def test_query_result_mutation_raises() -> None:
    result = _query_result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.row_count = 2  # type: ignore[misc]


def test_query_result_constructs_with_valid_fields() -> None:
    result = _query_result()
    assert result.columns == ["n"]
    assert result.rows == [["obl_risk_a1b2c3"]]
    assert result.row_count == 1


def test_query_result_accepts_empty_rows_and_columns() -> None:
    result = QueryResult(columns=[], rows=[], row_count=0)
    assert result.columns == []
    assert result.rows == []
    assert result.row_count == 0
