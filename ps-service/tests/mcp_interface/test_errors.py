"""Tests for `ps_service.mcp_interface.errors` (PLAN_REVIEWED.md §5, Batch 1).

Each error type: subclasses `Exception`, round-trips its message, and is
importable both from its own module and re-exported from the package
front door `ps_service.mcp_interface`.
"""

from __future__ import annotations

from ps_service.mcp_interface import (
    McpGraphUnavailableError,
    McpResourceUnavailableError,
)
from ps_service.mcp_interface import errors as errors_module


def test_graph_unavailable_error_subclasses_exception_and_keeps_message() -> None:
    exc = McpGraphUnavailableError("the policy graph database is not reachable")

    assert isinstance(exc, Exception)
    assert str(exc) == "the policy graph database is not reachable"


def test_resource_unavailable_error_subclasses_exception_and_keeps_message() -> None:
    exc = McpResourceUnavailableError("the ps-domain-concepts resource is currently unavailable")

    assert isinstance(exc, Exception)
    assert str(exc) == "the ps-domain-concepts resource is currently unavailable"


def test_error_types_are_distinct_and_not_a_shared_hierarchy() -> None:
    assert not issubclass(McpGraphUnavailableError, McpResourceUnavailableError)
    assert not issubclass(McpResourceUnavailableError, McpGraphUnavailableError)


def test_errors_module_exposes_both_types() -> None:
    assert errors_module.McpGraphUnavailableError is McpGraphUnavailableError
    assert errors_module.McpResourceUnavailableError is McpResourceUnavailableError
