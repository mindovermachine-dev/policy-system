"""Tests for the `verify-audit-log` MCP tool (gate, bypass refusal, result shape).

Hermetic: the access-role and audit stores are the fakes in `tests/authz/_fakes.py`, monkeypatched
onto `mcp_server`, exactly like the `list-audit-events` tests in `test_access_role_tools.py`.
The real chain recomputation is proven against a live Postgres in
`tests/audit/test_integrity_verify_live.py`; this file proves who may trigger it and what the
caller sees.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- same cross-package import as `test_access_role_tools.py`
    FakeAccessRoleStore,
    FakeAuditStore,
    RaisingAccessRoleStore,
)
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent

from ps_service.audit.errors import AuditPostgresUnavailableError
from ps_service.audit.models import AuditChainBreak, AuditChainVerification
from ps_service.authz.models import AccessRole
from ps_service.logging import configure, reset_for_tests
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_OWNER = "first-caller"
_ADMIN = "second-caller"
_MANAGER = "third-caller"
_NOBODY = "fourth-caller"
_ISSUER = "https://issuer.example.com/"
_BROKEN_AT = datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC)


@contextlib.contextmanager
def _verified_actor(*, sub: str) -> Generator[None]:
    token = AccessToken(
        token="test-token", client_id="test-client", scopes=[], subject=sub, claims={"iss": _ISSUER}
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def _factory(store: object) -> Callable[..., object]:
    def _make(_config: object, **_kwargs: object) -> object:
        return store

    return _make


def _call() -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("verify-audit-log", {}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture(autouse=True)
def _logging() -> Generator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    reset_for_tests()
    configure()
    yield
    reset_for_tests()


def _install(
    monkeypatch: pytest.MonkeyPatch, *, audit: FakeAuditStore, roles: object | None = None
) -> FakeAccessRoleStore:
    access = FakeAccessRoleStore(expected_owner=(_OWNER, _ISSUER))
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory(roles or access))
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", _factory(audit))
    return access


def test_the_local_test_bypass_is_refused_before_any_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No real authenticated caller -> the named refusal, and nothing was verified."""
    audit = FakeAuditStore()
    _install(monkeypatch, audit=audit)

    result = _call()  # no AccessToken bound: the local-test bypass

    assert result.is_error is False
    assert _text(result) == "error: access-role management requires a real authenticated caller"
    assert audit.verify_calls == []


def test_a_caller_with_no_access_role_is_rejected_and_nothing_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare AuthenticatedUser gets the named denial; the store is never reached."""
    audit = FakeAuditStore()
    access = _install(monkeypatch, audit=audit)
    access.bootstrap_first_owner((_OWNER, _ISSUER))  # the store is non-empty
    with _verified_actor(sub=_NOBODY):
        result = _call()

    assert result.is_error is False
    assert _text(result) == "error: You do not have the required access role for this action."
    assert audit.verify_calls == []


def test_a_policy_manager_only_caller_is_rejected_and_nothing_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PolicyManager is below SystemAdmin."""
    audit = FakeAuditStore()
    access = _install(monkeypatch, audit=audit)
    access.bootstrap_first_owner((_OWNER, _ISSUER))
    access.grant(
        actor=(_OWNER, _ISSUER), target=(_MANAGER, _ISSUER), access_role=AccessRole.POLICY_MANAGER
    )
    with _verified_actor(sub=_MANAGER):
        result = _call()

    assert _text(result) == "error: You do not have the required access role for this action."
    assert audit.verify_calls == []


def test_system_owner_and_system_admin_callers_get_the_intact_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An intact chain is reported with its extent, `first_break` null."""
    audit = FakeAuditStore(
        verify_result=AuditChainVerification(
            intact=True, rows_checked=12, head_chain_seq=12, first_break=None
        )
    )
    access = _install(monkeypatch, audit=audit)
    access.bootstrap_first_owner((_OWNER, _ISSUER))
    access.grant(
        actor=(_OWNER, _ISSUER), target=(_ADMIN, _ISSUER), access_role=AccessRole.SYSTEM_ADMIN
    )

    with _verified_actor(sub=_OWNER):
        owner_result = _call()
    with _verified_actor(sub=_ADMIN):
        admin_result = _call()

    expected = {"intact": True, "rows_checked": 12, "head_chain_seq": 12, "first_break": None}
    assert json.loads(_text(owner_result)) == expected
    assert json.loads(_text(admin_result)) == expected
    assert len(audit.verify_calls) == 2


def test_a_row_break_reports_kind_sequence_id_and_occurred_at_not_a_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first broken row is identified."""
    audit = FakeAuditStore(
        verify_result=AuditChainVerification(
            intact=False,
            rows_checked=10,
            head_chain_seq=10,
            first_break=AuditChainBreak(
                kind="row_hash_mismatch",
                chain_seq=4,
                event_id="11111111-1111-1111-1111-111111111111",
                occurred_at=_BROKEN_AT,
            ),
        )
    )
    _install(monkeypatch, audit=audit)
    with _verified_actor(sub=_OWNER):
        result = _call()

    assert json.loads(_text(result)) == {
        "intact": False,
        "rows_checked": 10,
        "head_chain_seq": 10,
        "first_break": {
            "kind": "row_hash_mismatch",
            "chain_seq": 4,
            "id": "11111111-1111-1111-1111-111111111111",
            "occurred_at": _BROKEN_AT.isoformat(),
        },
    }


def test_a_head_mismatch_reports_the_kind_and_the_head_sequence_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tail truncation has no broken row, so it names the head instead."""
    audit = FakeAuditStore(
        verify_result=AuditChainVerification(
            intact=False,
            rows_checked=8,
            head_chain_seq=10,
            first_break=AuditChainBreak(
                kind="head_mismatch", chain_seq=None, event_id=None, occurred_at=None
            ),
        )
    )
    _install(monkeypatch, audit=audit)
    with _verified_actor(sub=_OWNER):
        result = _call()

    assert json.loads(_text(result)) == {
        "intact": False,
        "rows_checked": 8,
        "head_chain_seq": 10,
        "first_break": {"kind": "head_mismatch", "head_chain_seq": 10},
    }


def test_an_unreachable_audit_store_is_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = FakeAuditStore(raise_on_verify=AuditPostgresUnavailableError("down"))
    _install(monkeypatch, audit=audit)
    with _verified_actor(sub=_OWNER):
        result = _call()

    assert _text(result) == "error: The authorization store is temporarily unavailable."


def test_an_unreachable_role_store_fails_closed_without_verifying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = FakeAuditStore()
    _install(monkeypatch, audit=audit, roles=RaisingAccessRoleStore())
    with _verified_actor(sub=_OWNER):
        result = _call()

    assert _text(result) == "error: The authorization store is temporarily unavailable."
    assert audit.verify_calls == []


def test_the_tool_takes_no_arguments() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.server.list_tools())}

    assert tools["verify-audit-log"].input_schema.get("properties") == {}


def test_the_docstring_states_the_gate_the_bypass_refusal_and_the_return_shape() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.server.list_tools())}
    description = " ".join((tools["verify-audit-log"].description or "").split())

    for needle in (
        "SystemOwner",
        "SystemAdmin",
        "local-test bypass",
        "intact",
        "first_break",
        "head_mismatch",
        "tamper-evident",
        "nothing is written",
    ):
        assert needle in description, needle
