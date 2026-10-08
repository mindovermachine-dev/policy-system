"""`invite_user_audited`: audit-first orchestration around the Authentik call (issue #195)."""

from __future__ import annotations

import json
import urllib.error
from typing import TYPE_CHECKING, Self

import pytest
from audit._fakes import InMemoryAuditStore

from ps_service.audit import (
    AuditContext,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
)
from ps_service.config import ServiceConfig
from ps_service.invitations.client import InvitationResult, create_invitation
from ps_service.invitations.errors import AuthentikInvitationError
from ps_service.invitations.service import invite_user_audited

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ps_service.logging.emitter import LogEmitter

    type ReadLines = Callable[[Path], list[dict[str, object]]]
    type MakeEmitter = Callable[..., tuple[LogEmitter, Path]]

_EMAIL = "target@example.com"
_ACTOR = ("admin-sub", "https://idp.example.com/")


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
        authentik_api_token="tok",
        authentik_base_url="https://authentik.example.com",
    )


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _transport_returning(body: bytes) -> Callable[..., _Response]:
    def _transport(request: object, /, *, timeout: float) -> _Response:
        del request, timeout
        return _Response(body)

    return _transport


def _transport_raising(error: Exception) -> Callable[..., _Response]:
    def _transport(request: object, /, *, timeout: float) -> _Response:
        del request, timeout
        raise error

    return _transport


type _Sender = Callable[[ServiceConfig, str], InvitationResult]


def _sender(transport: Callable[..., _Response]) -> _Sender:
    def _send(config: ServiceConfig, email: str) -> InvitationResult:
        return create_invitation(config, email, transport=transport)  # pyright: ignore[reportArgumentType]

    return _send


def _audit(store: InMemoryAuditStore) -> AuditContext:
    return AuditContext(actor=_ACTOR, store=store)


def test_invite_writes_applied_row_before_calling_authentik() -> None:
    store = InMemoryAuditStore()

    def _send(config: ServiceConfig, email: str) -> InvitationResult:
        del config, email
        store.events.append("authentik_call")
        return InvitationResult(itoken="t", invite_url="u")

    result = invite_user_audited(_config(), _EMAIL, audit=_audit(store), send_invitation=_send)

    assert result == InvitationResult(itoken="t", invite_url="u")
    assert store.events == ["audit:user.invite:applied", "authentik_call"]
    (row,) = store.rows
    assert (row.actor_subject, row.actor_issuer) == _ACTOR
    assert (row.action, row.resource_type, row.resource_id) == ("user.invite", "user", _EMAIL)
    assert row.outcome == "applied"
    assert row.details == {"invitee_email": _EMAIL}


def test_invite_failure_writes_failed_row_with_upstream_http_error_code() -> None:
    store = InMemoryAuditStore()
    error = urllib.error.HTTPError("http://x", 503, "unavailable", None, None)  # pyright: ignore[reportArgumentType]
    send = _sender(_transport_raising(error))

    with pytest.raises(AuthentikInvitationError):
        invite_user_audited(_config(), _EMAIL, audit=_audit(store), send_invitation=send)

    assert [r.outcome for r in store.rows] == ["applied", "failed"]
    assert store.rows[1].details == {"invitee_email": _EMAIL, "reason_code": "upstream_http_error"}


def test_invite_failure_on_transport_error_writes_upstream_unreachable() -> None:
    store = InMemoryAuditStore()
    send = _sender(_transport_raising(OSError("connection refused host=10.0.0.1")))

    with pytest.raises(AuthentikInvitationError):
        invite_user_audited(_config(), _EMAIL, audit=_audit(store), send_invitation=send)

    assert store.rows[1].details["reason_code"] == "upstream_unreachable"
    assert "10.0.0.1" not in json.dumps([r.details for r in store.rows])


def test_invite_malformed_authentik_payload_writes_unexpected_error_not_keyerror() -> None:
    store = InMemoryAuditStore()
    send = _sender(_transport_returning(json.dumps({"no_pk": 1}).encode()))

    with pytest.raises(AuthentikInvitationError):
        invite_user_audited(_config(), _EMAIL, audit=_audit(store), send_invitation=send)

    assert store.rows[1].outcome == "failed"
    assert store.rows[1].details["reason_code"] == "unexpected_error"


def test_invite_does_not_call_authentik_when_the_opening_row_cannot_be_written() -> None:
    store = InMemoryAuditStore(fail_on_outcome={"applied": AuditPostgresUnavailableError("x")})
    calls: list[str] = []

    def _send(config: ServiceConfig, email: str) -> InvitationResult:
        del config
        calls.append(email)
        return InvitationResult(itoken="t", invite_url="u")

    with pytest.raises(AuditTrailUnavailableError):
        invite_user_audited(_config(), _EMAIL, audit=_audit(store), send_invitation=_send)

    assert calls == []
    assert store.rows == []


def test_invite_raises_authentik_error_and_logs_when_failed_row_cannot_be_written(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore(fail_on_outcome={"failed": AuditPersistenceError("boom")})
    send = _sender(_transport_raising(OSError("down")))

    # the caller still sees the Authentik error, not the audit failure
    with pytest.raises(AuthentikInvitationError):
        invite_user_audited(
            _config(), _EMAIL, audit=_audit(store), send_invitation=send, emitter=emitter
        )

    emitter.flush()
    lines = read_lines(log_path)
    assert [line["action"] for line in lines] == ["audit_terminal_failed"]
    assert lines[0]["reason"] == "AuditPersistenceError"
    assert _EMAIL not in json.dumps(lines)
