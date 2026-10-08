"""The one MCP audit-context resolution shared by every audited tool (issue #218).

REST resolves the audit actor once in `provide_audit_context`; MCP does it in `_audit_context`.
These tests pin that helper's contract, and that `mcp_server.py` has no other copy of it.
"""

from __future__ import annotations

import inspect

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory

from ps_service.audit import AuditContext
from ps_service.config import ServiceConfig
from ps_service.mcp_interface import mcp_server

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
def audit_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:
    store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    return store


def test_returns_a_context_for_the_verified_caller(audit_store: InMemoryAuditStore) -> None:
    result = mcp_server._audit_context(  # pyright: ignore[reportPrivateUsage]  # the helper under test
        _config(is_local_test_bypass_active=False), _ACTOR
    )

    assert result == AuditContext(_ACTOR, audit_store)


def test_verified_caller_wins_over_the_bypass_sentinel(audit_store: InMemoryAuditStore) -> None:
    result = mcp_server._audit_context(  # pyright: ignore[reportPrivateUsage]  # the helper under test
        _config(is_local_test_bypass_active=True), _ACTOR
    )

    assert result == AuditContext(_ACTOR, audit_store)


def test_attributes_to_the_bypass_sentinel_when_bypass_is_active_without_a_caller(
    audit_store: InMemoryAuditStore,
) -> None:
    result = mcp_server._audit_context(  # pyright: ignore[reportPrivateUsage]  # the helper under test
        _config(is_local_test_bypass_active=True), None
    )

    assert result == AuditContext(_BYPASS_ACTOR, audit_store)


@pytest.mark.usefixtures("audit_store")
def test_returns_the_authenticated_caller_message_without_a_caller_and_without_bypass() -> None:
    result = mcp_server._audit_context(  # pyright: ignore[reportPrivateUsage]  # the helper under test
        _config(is_local_test_bypass_active=False), None
    )

    assert result == mcp_server._CATALOG_SOURCE_REQUIRES_AUTHENTICATED_CALLER_MESSAGE  # pyright: ignore[reportPrivateUsage]  # the message under test


def test_mcp_server_resolves_the_audit_actor_in_exactly_one_place() -> None:
    """AC-BI-003: no tool re-inlines the resolve-and-catch block or builds its own context."""
    source = inspect.getsource(mcp_server)

    assert source.count("resolve_audit_actor(") == 1
    assert source.count("AuditContext(") == 1
