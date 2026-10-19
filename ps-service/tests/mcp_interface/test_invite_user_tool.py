"""Tests for the registered `invite-user` MCP tool.

A `SystemAdmin`-or-above caller succeeds and gets
`itoken`/`invite_url`; a malformed/missing email is rejected at
the schema layer before any Authentik call; an
Authentik-unreachable error is returned sanitized (no token, no stack
trace substring); a successful invite leaves the invitee's email and
the actor out of the log, and its `user.invite` audit row records both.
"""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from invitations.test_client import (
    _HttpErrorTransport,  # pyright: ignore[reportPrivateUsage]  -- reuse `create_invitation`'s own fake-transport doubles verbatim (mirrors this file's existing cross-package reuse of `test_catalog_source_authz_gate`'s fixtures) rather than re-declaring them
    _RecordingTransport,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_catalog_source_authz_gate import (
    _CALLER_ISSUER,  # pyright: ignore[reportPrivateUsage]  -- reuse this issue's own gate-test fixtures verbatim rather than re-declaring them
    _NEW_SYSTEM_ADMIN_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.audit import AuditPostgresUnavailableError, AuditQueryFilters
from ps_service.authz.models import AccessRole
from ps_service.authz.service import list_audit_events
from ps_service.invitations.client import AuthentikTransport, create_invitation
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


@pytest.fixture(name="audit_store", autouse=True)
def _audit_store_fixture(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:  # pyright: ignore[reportUnusedFunction]  # autouse + injected by name
    """Every invite writes `user.invite` rows: keep them off a real Postgres."""
    store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    return store


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
    """A `SystemAdmin`-or-above caller gets `itoken` and `invite_url`.

    Exercises the REAL `ps_service.invitations.client.create_invitation`
    (`create_invitation` already has its own unused
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
    sent = transport.requests[0].data
    assert isinstance(sent, bytes)
    expires = json.loads(sent)["expires"]
    assert isinstance(expires, str)
    assert datetime.fromisoformat(expires).tzinfo is not None


@pytest.mark.parametrize("bad_email", ["not-an-email", "", "missing-domain@"])
def test_malformed_email_rejected_before_any_authentik_call(
    bad_email: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejected at the MCP schema layer -- confirmed via
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
    """The returned string never contains the configured
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


def test_successful_invite_logs_no_invitee_email_and_no_invitations_entry(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """A successful invite leaves the invitee's email and the actor out of the log.

    The `user.invite` audit row is the record of who invited whom; the log carries only the
    generic `mcp_interface` entries for the tool call.
    """
    emitter = configure()
    _as_admin(monkeypatch)
    _use_transport(monkeypatch, _RecordingTransport(json.dumps({"pk": "tok-audit"}).encode()))

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert result.is_error is False
    emitter.flush()
    raw = resolve_default_log_path().read_text(encoding="utf-8")
    assert _EMAIL not in raw
    all_lines = read_lines(resolve_default_log_path())
    assert all_lines
    assert [line for line in all_lines if line.get("component") == "invitations"] == []
    assert [line for line in all_lines if "actor" in line] == []


def _as_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant `_NEW_SYSTEM_ADMIN_SUBJECT` SystemAdmin in a fake role store; set Authentik config."""
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", _TOKEN)
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", _BASE_URL)
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_NEW_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(store))


def _use_transport(monkeypatch: pytest.MonkeyPatch, transport: AuthentikTransport) -> None:
    monkeypatch.setattr(
        mcp_server,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )


def test_invite_user_audits_the_verified_actor_subject_and_issuer(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """One `applied` `user.invite` row by the verified caller, email only."""
    configure()
    _as_admin(monkeypatch)
    _use_transport(monkeypatch, _RecordingTransport(json.dumps({"pk": "tok-1"}).encode()))

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert result.is_error is False
    (row,) = audit_store.rows
    assert (row.actor_subject, row.actor_issuer) == (_NEW_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER)
    assert (row.action, row.resource_type, row.resource_id, row.outcome) == (
        "user.invite",
        "user",
        _EMAIL,
        "applied",
    )
    assert row.details == {"invitee_email": _EMAIL}


def test_invite_user_under_local_test_bypass_audits_as_system_local_test_bypass(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", _TOKEN)
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", _BASE_URL)
    _use_transport(monkeypatch, _RecordingTransport(json.dumps({"pk": "tok-1"}).encode()))

    result = _call({"email": _EMAIL})

    assert result.is_error is False
    (row,) = audit_store.rows
    assert (row.actor_subject, row.actor_issuer) == (
        "system:local-test-bypass",
        "system:local-test-bypass",
    )


def test_invite_user_returns_error_and_calls_nothing_when_audit_unavailable(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """Fail closed with an `error: ` response; Authentik is never contacted."""
    configure()
    _as_admin(monkeypatch)
    transport = _RecordingTransport(json.dumps({"pk": "unused"}).encode())
    _use_transport(monkeypatch, transport)
    audit_store.fail_on_outcome["applied"] = AuditPostgresUnavailableError("host=db.internal")

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert _text(result) == (
        "error: The audit trail is temporarily unavailable; the operation was not performed."
    )
    assert transport.requests == []
    assert audit_store.rows == []


def test_invite_user_failure_records_a_failed_row_with_a_reason_code(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """An Authentik failure adds a `failed` row; no free text in it."""
    configure()
    _as_admin(monkeypatch)
    _use_transport(monkeypatch, _HttpErrorTransport(503, "unavailable"))

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert _text(result).startswith("error:")
    assert [r.outcome for r in audit_store.rows] == ["applied", "failed"]
    assert audit_store.rows[1].details == {
        "invitee_email": _EMAIL,
        "reason_code": "upstream_http_error",
    }


def test_invite_user_rows_never_contain_the_itoken_or_invite_url_or_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
    audit_store: InMemoryAuditStore,
    read_lines: ReadLines,
) -> None:
    """The secret pk, the redemption URL and the API token appear in no row or log."""
    emitter = configure()
    _as_admin(monkeypatch)
    _use_transport(monkeypatch, _RecordingTransport(json.dumps({"pk": "SECRET-PK-123"}).encode()))

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        result = _call({"email": _EMAIL})

    assert "SECRET-PK-123" in _text(result)  # the caller still receives it
    emitter.flush()
    log_lines = read_lines(resolve_default_log_path())
    assert any(line.get("action") == "invite_user" for line in log_lines)
    haystack = json.dumps([vars(r) for r in audit_store.rows]) + json.dumps(log_lines)
    for secret in ("SECRET-PK-123", "itoken=", "/if/flow/ps-invite-enrollment/", _TOKEN):
        assert secret not in haystack


def test_invite_user_rows_are_returned_by_list_audit_events_by_action_and_resource_id(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    configure()
    _as_admin(monkeypatch)
    _use_transport(monkeypatch, _RecordingTransport(json.dumps({"pk": "tok-1"}).encode()))
    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        _call({"email": _EMAIL})
        _call({"email": "other@example.com"})

    page = list_audit_events(
        (_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        filters=AuditQueryFilters(action="user.invite", resource_type="user", resource_id=_EMAIL),
        cursor=None,
        page_size=10,
        access_role_store=_seeded_store(),
        audit_store=audit_store,
    )

    assert [(e.action, e.resource_id, e.details) for e in page.events] == [
        ("user.invite", _EMAIL, {"invitee_email": _EMAIL})
    ]
