"""Tests for the started/terminal log entries every MCP tool call emits.

Drives `invite-user` through the real `server.call_tool` path: a `SystemAdmin` role gate, a body
that can succeed, return an `error:` string or raise, and a local-test bypass that skips the gate.
On every branch the entries are correlated by one `run_id` and carry no caller identity; the
audit trail is where the caller is attributed.
"""

from __future__ import annotations

import asyncio
import functools
import json
from typing import TYPE_CHECKING, NoReturn

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory
from invitations.test_client import (
    _HttpErrorTransport,  # pyright: ignore[reportPrivateUsage]  -- reuse `create_invitation`'s own fake-transport doubles verbatim rather than re-declaring them
    _RecordingTransport,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_catalog_source_authz_gate import (
    _ACCESS_DENIED_MESSAGE,  # pyright: ignore[reportPrivateUsage]  -- reuse the gate tests' fixtures verbatim rather than re-declaring them
    _CALLER_ISSUER,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _NEW_SYSTEM_ADMIN_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _NON_ADMIN_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.authz.models import AccessRole
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.invitations.client import AuthentikTransport, create_invitation
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import catalog_tools, mcp_server, tool_core

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_EMAIL = "target@example.com"
_GENERIC_ERROR = "error: an unexpected error occurred"


@pytest.fixture(autouse=True)
def _audit_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Gate and invite audit rows go to an in-memory store, never a real Postgres."""
    store = InMemoryAuditStore()
    monkeypatch.setattr(tool_core, "PsycopgAuditStore", audit_store_factory(store))
    return store


def _call_invite_user() -> str:
    result = asyncio.run(mcp_server.server.call_tool("invite-user", {"email": _EMAIL}))
    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _authentik_configured(monkeypatch: pytest.MonkeyPatch, transport: AuthentikTransport) -> None:
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", "test-authentik-token")
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", "https://authentik.example.com")
    monkeypatch.setattr(
        catalog_tools,
        "create_invitation",
        functools.partial(create_invitation, transport=transport),
    )


def _as_system_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _seeded_store()
    store.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_NEW_SYSTEM_ADMIN_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.SYSTEM_ADMIN,
    )
    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _fake_store_factory(store))


def _invite_user_lines(read_lines: ReadLines) -> tuple[str, list[dict[str, object]]]:
    """Return the raw log text and the `invite_user` tool-call entries in it."""
    log_path = resolve_default_log_path()
    raw = log_path.read_text(encoding="utf-8")
    lines = [
        line
        for line in read_lines(log_path)
        if line.get("component") == "mcp_interface" and line.get("action") == "invite_user"
    ]
    return raw, lines


def test_tool_call_refused_by_its_role_gate_logs_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    monkeypatch.setattr(tool_core, "PsycopgAccessRoleStore", _fake_store_factory(_seeded_store()))

    with _verified_actor(sub=_NON_ADMIN_SUBJECT):
        text = _call_invite_user()

    assert text == _ACCESS_DENIED_MESSAGE
    emitter.flush()
    raw, lines = _invite_user_lines(read_lines)
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert len({line["run_id"] for line in lines}) == 1
    assert all("principal" not in line for line in lines), lines
    assert _NON_ADMIN_SUBJECT not in raw


def test_successful_tool_call_entries_carry_no_principal_and_share_one_run_id(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    _as_system_admin(monkeypatch)
    _authentik_configured(monkeypatch, _RecordingTransport(json.dumps({"pk": "tok-1"}).encode()))

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        text = _call_invite_user()

    assert json.loads(text)["itoken"] == "tok-1"
    emitter.flush()
    raw, lines = _invite_user_lines(read_lines)
    assert [line["outcome"] for line in lines] == ["started", "succeeded"]
    assert len({line["run_id"] for line in lines}) == 1
    assert all("principal" not in line for line in lines), lines
    assert _NEW_SYSTEM_ADMIN_SUBJECT not in raw
    assert _EMAIL not in raw


def test_tool_call_returning_an_error_string_logs_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    _as_system_admin(monkeypatch)
    _authentik_configured(monkeypatch, _HttpErrorTransport(503, "Service Unavailable"))

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        text = _call_invite_user()

    assert text.startswith("error:")
    emitter.flush()
    raw, lines = _invite_user_lines(read_lines)
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert len({line["run_id"] for line in lines}) == 1
    assert all("principal" not in line for line in lines), lines
    assert _NEW_SYSTEM_ADMIN_SUBJECT not in raw


def test_tool_call_whose_body_raises_logs_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> NoReturn:
        message = "authentik client blew up"
        raise RuntimeError(message)

    emitter = configure()
    _as_system_admin(monkeypatch)
    monkeypatch.setenv("PS_AUTHENTIK_API_TOKEN", "test-authentik-token")
    monkeypatch.setenv("PS_AUTHENTIK_BASE_URL", "https://authentik.example.com")
    monkeypatch.setattr(catalog_tools, "create_invitation", _raise)

    with _verified_actor(sub=_NEW_SYSTEM_ADMIN_SUBJECT):
        text = _call_invite_user()

    assert text == _GENERIC_ERROR
    emitter.flush()
    raw, lines = _invite_user_lines(read_lines)
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert lines[1]["exception_type"] == "RuntimeError"
    assert len({line["run_id"] for line in lines}) == 1
    assert all("principal" not in line for line in lines), lines
    assert _NEW_SYSTEM_ADMIN_SUBJECT not in raw


def test_tool_call_under_the_local_test_bypass_logs_no_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    emitter = configure()
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _authentik_configured(monkeypatch, _RecordingTransport(json.dumps({"pk": "tok-2"}).encode()))

    text = _call_invite_user()

    assert json.loads(text)["itoken"] == "tok-2"
    emitter.flush()
    _raw, lines = _invite_user_lines(read_lines)
    assert [line["outcome"] for line in lines] == ["started", "succeeded"]
    assert len({line["run_id"] for line in lines}) == 1
    assert all("principal" not in line for line in lines), lines
    assert all(
        LOCAL_TEST_PRINCIPAL_ID not in json.dumps(line)
        for line in read_lines(resolve_default_log_path())
    )
