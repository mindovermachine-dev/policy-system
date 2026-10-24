"""The one MCP audit-context resolution shared by every audited tool.

REST resolves the audit actor once in `provide_audit_context`; MCP does it in `_audit_context`.
These tests pin that helper's contract, and that `mcp_server.py` has no other copy of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory

from mcp_interface._environment import fail_closed_environment
from mcp_interface._source_modules import tool_source_text
from ps_service.audit import AuditContext
from ps_service.config import ServiceConfig
from ps_service.mcp_interface import tool_core as mcp_server

if TYPE_CHECKING:
    from ps_service.mcp_interface.tool_gates import GateEnvironment

_ACTOR = ("sub-1", "https://issuer.example.com")
_BYPASS_ACTOR = ("system:local-test-bypass", "system:local-test-bypass")


def _config(*, is_local_test_bypass_active: bool) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=is_local_test_bypass_active,
    )


@pytest.fixture
def audit_store() -> InMemoryAuditStore:
    return InMemoryAuditStore()


def _environment(store: InMemoryAuditStore) -> GateEnvironment:
    return fail_closed_environment(audit_store_factory=audit_store_factory(store))


def test_returns_a_context_for_the_verified_caller(audit_store: InMemoryAuditStore) -> None:
    result = mcp_server.audit_context(  # pyright: ignore[reportPrivateUsage]  # the helper under test
        _config(is_local_test_bypass_active=False), _ACTOR, environment=_environment(audit_store)
    )

    assert result == AuditContext(_ACTOR, audit_store)


def test_verified_caller_wins_over_the_bypass_sentinel(audit_store: InMemoryAuditStore) -> None:
    result = mcp_server.audit_context(  # pyright: ignore[reportPrivateUsage]  # the helper under test
        _config(is_local_test_bypass_active=True), _ACTOR, environment=_environment(audit_store)
    )

    assert result == AuditContext(_ACTOR, audit_store)


def test_attributes_to_the_bypass_sentinel_when_bypass_is_active_without_a_caller(
    audit_store: InMemoryAuditStore,
) -> None:
    result = mcp_server.audit_context(  # pyright: ignore[reportPrivateUsage]  # the helper under test
        _config(is_local_test_bypass_active=True), None, environment=_environment(audit_store)
    )

    assert result == AuditContext(_BYPASS_ACTOR, audit_store)


def test_returns_the_authenticated_caller_message_without_a_caller_and_without_bypass() -> None:
    result = mcp_server.audit_context(  # pyright: ignore[reportPrivateUsage]  # the helper under test
        _config(is_local_test_bypass_active=False), None, environment=fail_closed_environment()
    )

    assert result == mcp_server._CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE  # pyright: ignore[reportPrivateUsage]  # the message under test


def test_mcp_server_resolves_the_audit_actor_in_exactly_one_place() -> None:
    """No tool re-inlines the resolve-and-catch block or builds its own context."""
    source = tool_source_text()

    assert source.count("resolve_audit_actor(") == 1
    assert source.count("AuditContext(") == 1
