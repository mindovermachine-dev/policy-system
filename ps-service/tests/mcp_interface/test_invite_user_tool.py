"""Tests for the registered `invite-user` MCP tool (issue #140).

AC-BI-005 (a `SystemAdmin`-or-above caller succeeds, returns
`itoken`/`invite_url`), AC-BI-007 (a malformed/missing email is rejected at
the schema layer before any Authentik call), AC-BI-008 (an
Authentik-unreachable error is returned sanitized -- no token, no stack
trace substring), AC-BI-009 (a successful invite is recorded to the logging
output with actor, target email, timestamp, outcome).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_catalog_source_authz_gate import (
    _CALLER_ISSUER,  # pyright: ignore[reportPrivateUsage]  -- reuse this issue's own gate-test fixtures verbatim rather than re-declaring them
    _NEW_SYSTEM_ADMIN_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _NON_ADMIN_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.authz.models import AccessRole
from ps_service.invitations.client import InvitationResult
from ps_service.invitations.errors import AuthentikInvitationError
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_EMAIL = "target@example.com"
_TOKEN = "test-authentik-token"


def _call(args: dict[str, object]) -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("invite-user", args))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_system_admin_caller_succeeds_and_returns_itoken_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005."""
    configure()
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_NEW_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    def _canned_create_invitation(*_args: object, **_kwargs: object) -> InvitationResult:
        return InvitationResult(
            itoken="tok-abc",
            invite_url="https://authentik.example.com/if/flow/ps-invite-enrollment/?itoken=tok-abc",
        )

    monkeypatch.setattr(mcp_server, "create_invitation", _canned_create_invitation)

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert result.is_error is False
    assert json.loads(_text(result)) == {
        "itoken": "tok-abc",
        "invite_url": "https://authentik.example.com/if/flow/ps-invite-enrollment/?itoken=tok-abc",
    }


@pytest.mark.parametrize("bad_email", ["not-an-email", "", "missing-domain@"])
def test_malformed_email_rejected_before_any_authentik_call(
    bad_email: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-BI-007: rejected at the MCP schema layer -- confirmed via
    `test_restore_instrument_tool.py`'s own documented behavior, a bare
    in-process `server.call_tool` propagates a schema rejection as a raised
    `ToolError`, never reaching the tool's own body.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    calls: list[str] = []

    def _never_expected(*_args: object, email: str = "", **_kwargs: object) -> InvitationResult:
        calls.append(email)
        return InvitationResult(itoken="unused", invite_url="unused")

    monkeypatch.setattr(mcp_server, "create_invitation", _never_expected)

    with pytest.raises(ToolError):
        _call({"email": bad_email})

    assert calls == []


def test_authentik_unreachable_returns_sanitized_error_no_stack_trace_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-008: the returned string never contains the configured
    `authentik_api_token` value or a traceback substring.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()

    def _raise(*_args: object, **_kwargs: object) -> InvitationResult:
        raise AuthentikInvitationError("Authentik invitation request failed: HTTP 503")

    monkeypatch.setattr(mcp_server, "create_invitation", _raise)

    result = _call({"email": _EMAIL})

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error:")
    assert _TOKEN not in text
    assert "Traceback" not in text


def test_successful_invite_emits_audit_log_entry_with_actor_email_outcome(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-009: a successful invite records actor, target email, timestamp,
    and outcome as its own `component="invitations"` audit entry -- distinct
    from (additional to) the generic `mcp_interface` started/succeeded pair
    `_run_mcp_action` already emits for every tool call.
    """
    emitter = configure()
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_NEW_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    def _canned_create_invitation(*_args: object, **_kwargs: object) -> InvitationResult:
        return InvitationResult(
            itoken="tok-audit",
            invite_url="https://authentik.example.com/if/flow/ps-invite-enrollment/?itoken=tok-audit",
        )

    monkeypatch.setattr(mcp_server, "create_invitation", _canned_create_invitation)

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert result.is_error is False

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    audit_lines = [
        line
        for line in all_lines
        if line.get("component") == "invitations" and line.get("action") == "invite_user"
    ]
    assert len(audit_lines) == 1
    (entry,) = audit_lines
    assert entry["entity_id"] == _EMAIL
    assert entry["outcome"] == "created"
    assert isinstance(entry["timestamp"], float)
    assert entry["actor"] == _NEW_SYSTEM_ADMIN_SUBJECT


def test_denied_caller_does_not_emit_invite_created_audit_entry(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """The audit entry is emitted from `invite_user`'s success path only --
    a denied caller (never reaching `create_invitation`) must not produce
    one.
    """
    emitter = configure()
    store = _seeded_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    def _never_expected(*_args: object, **_kwargs: object) -> InvitationResult:
        message = "create_invitation must not be called when the gate rejects the caller"
        raise AssertionError(message)

    monkeypatch.setattr(mcp_server, "create_invitation", _never_expected)

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert result.is_error is False
    assert _text(result).startswith("error:")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    audit_lines = [
        line
        for line in all_lines
        if line.get("component") == "invitations" and line.get("action") == "invite_user"
    ]
    assert audit_lines == []
