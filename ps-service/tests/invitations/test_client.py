"""Tests for `ps_service.invitations.client.create_invitation` (issue #140).

Mirrors `tests/curated_source/test_http_fetch.py`'s own fake-transport
pattern exactly -- mocking at the transport boundary (L2 Testing Patterns),
never reaching real network. Per CHANGES.md Appendix C, the happy-path test
asserts the *exact* outgoing JSON body, not just "a request was sent" --
a future correction against real Authentik should be a one-line diff.
"""

from __future__ import annotations

import email.message
import json
import urllib.error
import urllib.request
from typing import NoReturn, Self

import pytest

from ps_service.config import ServiceConfig
from ps_service.invitations.client import InvitationResult, create_invitation
from ps_service.invitations.errors import AuthentikInvitationError

_TOKEN = "test-authentik-token"
_BASE_URL = "https://authentik.example.com"
_EMAIL = "target@example.com"


def _config(authentik_public_url: str | None = None) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=True,
        authentik_api_token=_TOKEN,
        authentik_base_url=_BASE_URL,
        authentik_public_url=authentik_public_url,
    )


class _FakeResponse:
    """A minimal stand-in for what `urllib.request.urlopen` returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _RecordingTransport:
    """Captures the exact `Request` it was called with."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return _FakeResponse(self._body)


class _HttpErrorTransport:
    def __init__(self, code: int, reason: str) -> None:
        self._code = code
        self._reason = reason

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
        raise urllib.error.HTTPError(
            request.full_url, self._code, self._reason, email.message.Message(), None
        )


class _FailingTransport:
    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
        raise ConnectionRefusedError("connection refused")


def test_create_invitation_sends_exact_request_body_and_headers() -> None:
    """CHANGES.md Appendix C: assert exact JSON body equality (bar the random
    `name` suffix), the `Authorization` header, and the HTTP method.
    """
    transport = _RecordingTransport(json.dumps({"pk": "abc123"}).encode())

    create_invitation(_config(), _EMAIL, transport=transport)

    assert len(transport.requests) == 1
    request = transport.requests[0]
    request_data = request.data
    assert isinstance(request_data, bytes)
    body = json.loads(request_data)
    name = body.pop("name")
    assert isinstance(name, str)
    assert name.startswith("ps-invite-")
    assert body == {"single_use": True, "fixed_data": {"email": _EMAIL}}
    assert request.get_header("Authorization") == f"Bearer {_TOKEN}"
    assert request.get_method() == "POST"
    assert request.full_url == f"{_BASE_URL}/api/v3/stages/invitation/invitations/"


def test_create_invitation_returns_itoken_and_invite_url_from_response_pk() -> None:
    transport = _RecordingTransport(json.dumps({"pk": "abc123"}).encode())

    result = create_invitation(_config(), _EMAIL, transport=transport)

    assert result == InvitationResult(
        itoken="abc123",
        invite_url=f"{_BASE_URL}/if/flow/ps-invite-enrollment/?itoken=abc123",
    )


def test_create_invitation_builds_link_from_public_url_when_set() -> None:
    """Issue #165 OD-1=B: link uses the public URL, the API call still goes to the base URL."""
    public_url = "https://ps.example.com/auth"
    transport = _RecordingTransport(json.dumps({"pk": "abc123"}).encode())

    result = create_invitation(_config(public_url), _EMAIL, transport=transport)

    assert transport.requests[0].full_url == f"{_BASE_URL}/api/v3/stages/invitation/invitations/"
    assert result.invite_url == f"{public_url}/if/flow/ps-invite-enrollment/?itoken=abc123"


def test_create_invitation_wraps_http_error_naming_status_never_the_token() -> None:
    transport = _HttpErrorTransport(403, "Forbidden")

    with pytest.raises(AuthentikInvitationError) as excinfo:
        create_invitation(_config(), _EMAIL, transport=transport)

    assert "403" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)


def test_create_invitation_wraps_generic_transport_exception_never_leaking_the_token() -> None:
    transport = _FailingTransport()

    with pytest.raises(AuthentikInvitationError) as excinfo:
        create_invitation(_config(), _EMAIL, transport=transport)

    assert _TOKEN not in str(excinfo.value)
