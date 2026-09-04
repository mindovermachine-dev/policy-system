"""Tests for ps_service.query_engine.errors."""

from __future__ import annotations

from ps_service.query_engine.cypher_query import (
    _GRAPH_UNSEEDED_DETAIL,  # pyright: ignore[reportPrivateUsage]  # test pins the exact sanitized detail string (AC-BI-015)
)
from ps_service.query_engine.errors import (
    GraphUnseededError,
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


def test_graph_unseeded_error_is_exception_subclass() -> None:
    assert issubclass(GraphUnseededError, Exception)


def test_graph_unseeded_error_message_is_exactly_the_constructor_argument() -> None:
    error = GraphUnseededError(_GRAPH_UNSEEDED_DETAIL)
    assert str(error) == _GRAPH_UNSEEDED_DETAIL


def test_graph_unseeded_error_message_contains_no_host_port_or_path_detail() -> None:
    """AC-BI-015: the message is a fixed, sanitized string -- a direct
    string-content assertion, mirroring `mcp_interface.mcp_server
    ._GRAPH_UNAVAILABLE_DETAIL`'s own sanitization test shape
    (`tests/mcp_interface/test_cypher_tool.py`).
    """
    message = str(GraphUnseededError(_GRAPH_UNSEEDED_DETAIL))

    assert message == "the policy graph has no seeded content yet"
    assert "/" not in message
    assert ":" not in message
    assert not any(char.isdigit() for char in message)
