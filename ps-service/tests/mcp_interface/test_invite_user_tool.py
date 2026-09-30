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
import functools
import json
from typing import TYPE_CHECKING

import pytest
from invitations.test_client import (
    _HttpErrorTransport,  # pyright: ignore[reportPrivateUsage]  -- reuse `create_invitation`'s own fake-transport doubles verbatim (mirrors this file's existing cross-package reuse of `test_catalog_source_authz_gate`'s fixtures) rather than re-declaring them
    _RecordingTransport,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
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
from ps_service.invitations.client import create_invitation
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_EMAIL = "target@example.com"
_TOKEN = "test-authentik-token"
_BASE_URL = "https://authentik.example.com"


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
    """AC-BI-005.

    Exercises the REAL `ps_service.invitations.client.create_invitation`
    (issue #163 Slice F fix: `create_invitation` already has its own unused
    `transport=` DI seam -- rewiring `mcp_server.create_invitation` to a
    `functools.partial` over the real function with a fake transport lets
    the tool's delegate run for real instead of being replaced wholesale).
    """
    configure()
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", _TOKEN)
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", _BASE_URL)
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_NEW_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    transport = _RecordingTransport(json.dumps({"pk": "tok-abc"}).encode())
    monkeypatch.setattr(
        mcp_server,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert result.is_error is False
    assert json.loads(_text(result)) == {
        "itoken": "tok-abc",
        "invite_url": "https://authentik.example.com/if/flow/ps-invite-enrollment/?itoken=tok-abc",
    }
    assert len(transport.requests) == 1


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
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", _TOKEN)
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", _BASE_URL)
    configure()

    transport = _RecordingTransport(json.dumps({"pk": "unused"}).encode())
    monkeypatch.setattr(
        mcp_server,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )

    with pytest.raises(ToolError):
        _call({"email": bad_email})

    assert transport.requests == []


def test_authentik_unreachable_returns_sanitized_error_no_stack_trace_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-008: the returned string never contains the configured
    `authentik_api_token` value or a traceback substring.

    A real HTTP 503 from the transport boundary, translated by the real
    `create_invitation`'s own error handling -- not a hand-raised
    `AuthentikInvitationError` standing in for it.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", _TOKEN)
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", _BASE_URL)
    configure()

    transport = _HttpErrorTransport(503, "Service Unavailable")
    monkeypatch.setattr(
        mcp_server,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )

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
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", _TOKEN)
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", _BASE_URL)
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_NEW_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    transport = _RecordingTransport(json.dumps({"pk": "tok-audit"}).encode())
    monkeypatch.setattr(
        mcp_server,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )

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
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", _TOKEN)
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", _BASE_URL)
    store = _seeded_store()
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))

    transport = _RecordingTransport(json.dumps({"pk": "unused"}).encode())
    monkeypatch.setattr(
        mcp_server,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert result.is_error is False
    assert _text(result).startswith("error:")
    assert transport.requests == []

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    audit_lines = [
        line
        for line in all_lines
        if line.get("component") == "invitations" and line.get("action") == "invite_user"
    ]
    assert audit_lines == []
