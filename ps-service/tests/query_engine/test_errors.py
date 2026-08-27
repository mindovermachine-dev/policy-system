"""Tests for ps_service.query_engine.errors."""

from __future__ import annotations

from ps_service.query_engine.errors import (
    QueryEngineExecutionError,
    WriteClauseRejectedError,
)


def test_write_clause_rejected_error_is_exception_subclass() -> None:
    assert issubclass(WriteClauseRejectedError, Exception)


def test_write_clause_rejected_error_message_is_exactly_the_constructor_argument() -> None:
    error = WriteClauseRejectedError("this command is read-only -- query contains a write clause")
    assert str(error) == "this command is read-only -- query contains a write clause"


def test_query_engine_execution_error_is_exception_subclass() -> None:
    assert issubclass(QueryEngineExecutionError, Exception)


def test_query_engine_execution_error_message_is_exactly_the_constructor_argument() -> None:
    error = QueryEngineExecutionError("Syntax error at offset 12")
    assert str(error) == "Syntax error at offset 12"
