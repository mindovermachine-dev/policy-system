"""AC-BI-002/AC-BI-001: PS Service refuses to start on absent config or failed discovery.

Issue #58. Slice 1 covers the fail-closed *presence* check (AC-BI-002).
Slice 2 (this file's `Test*Discovery*`-style tests below) covers the "both
set" branch: OIDC discovery fetch + asymmetric-algorithm allow-list math
(AC-BI-001, and AC-BI-009's discovery-time half) -- exercised via
`resolve_auth_context` directly with an injected fake `transport`, per
`ps_service.auth.discovery.DiscoveryTransport`'s DI seam. No real HTTP
server here (that is Slice 3's mock-OIDC-provider infrastructure).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Self

import pytest

from ps_service.auth.errors import AuthConfigurationError, AuthDiscoveryError
from ps_service.auth.models import AuthContext
from ps_service.auth.startup import resolve_auth_context
from ps_service.config import ServiceConfig
from ps_service.main import create_app


def _config(**overrides: object) -> ServiceConfig:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "is_local_test_bypass_active": False,
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


def test_create_app_raises_naming_both_vars_when_issuer_and_audience_both_unset() -> None:
    with pytest.raises(AuthConfigurationError) as excinfo:
        create_app(_config())

    message = str(excinfo.value)
    assert "PS_AUTH_ISSUER and PS_AUTH_AUDIENCE are unset" in message
    assert "PS_SERVICE_LOCAL_TEST_BYPASS" in message


def test_create_app_raises_naming_only_audience_when_issuer_is_set() -> None:
    with pytest.raises(AuthConfigurationError) as excinfo:
        create_app(_config(auth_issuer="https://issuer.example.com"))

    message = str(excinfo.value)
    assert "PS_AUTH_AUDIENCE is unset" in message
    assert "PS_SERVICE_LOCAL_TEST_BYPASS" in message


def test_create_app_raises_naming_only_issuer_when_audience_is_set() -> None:
    with pytest.raises(AuthConfigurationError) as excinfo:
        create_app(_config(auth_audience="https://api.example.com"))

    message = str(excinfo.value)
    assert "PS_AUTH_ISSUER is unset" in message
    assert "PS_SERVICE_LOCAL_TEST_BYPASS" in message


def test_create_app_does_not_raise_when_bypass_active_even_with_both_unset() -> None:
    app = create_app(_config(is_local_test_bypass_active=True))

    assert app.state.auth_context is None


_ISSUER = "https://issuer.example.com"
_AUDIENCE = "https://api.example.com"


class _FakeDiscoveryResponse:
    """A minimal stand-in for what `urllib.request.urlopen` returns: a
    context manager whose `read()` yields the body bytes.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _ScriptedDiscoveryTransport:
    """Returns a fixed discovery-document body, recording the request it received.

    Mocking at the transport boundary, per L2 Testing Patterns -- mirrors
    `tests/ingestion/adapters/cellar_eli/test_fetch.py`'s `_RecordingTransport`.
    """

    def __init__(self, document: dict[str, object]) -> None:
        self._body = json.dumps(document).encode("utf-8")
        self.requests: list[urllib.request.Request] = []

    def __call__(
        self, request: urllib.request.Request, /, *, timeout: float
    ) -> _FakeDiscoveryResponse:
        self.requests.append(request)
        return _FakeDiscoveryResponse(self._body)


class _FailingDiscoveryTransport:
    """Simulates a network-level discovery-fetch failure (e.g. connection refused)."""

    def __call__(
        self, request: urllib.request.Request, /, *, timeout: float
    ) -> _FakeDiscoveryResponse:
        raise urllib.error.URLError("connection refused")


def test_resolve_auth_context_builds_context_from_successful_discovery() -> None:
    transport = _ScriptedDiscoveryTransport(
        {
            "jwks_uri": f"{_ISSUER}/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
    )

    context = resolve_auth_context(
        _config(auth_issuer=_ISSUER, auth_audience=_AUDIENCE),
        transport=transport,
    )

    assert context == AuthContext(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        cli_client_id=None,
        scopes=(),
        jwks_uri=f"{_ISSUER}/jwks.json",
        allowed_algorithms=frozenset({"RS256"}),
    )
    assert len(transport.requests) == 1
    assert transport.requests[0].full_url == f"{_ISSUER}/.well-known/openid-configuration"


def test_resolve_auth_context_raises_naming_issuer_when_discovery_fetch_fails() -> None:
    with pytest.raises(AuthDiscoveryError) as excinfo:
        resolve_auth_context(
            _config(auth_issuer=_ISSUER, auth_audience=_AUDIENCE),
            transport=_FailingDiscoveryTransport(),
        )

    assert _ISSUER in str(excinfo.value)


def test_resolve_auth_context_raises_naming_issuer_when_jwks_uri_is_absent() -> None:
    transport = _ScriptedDiscoveryTransport({"id_token_signing_alg_values_supported": ["RS256"]})

    with pytest.raises(AuthDiscoveryError) as excinfo:
        resolve_auth_context(
            _config(auth_issuer=_ISSUER, auth_audience=_AUDIENCE),
            transport=transport,
        )

    assert _ISSUER in str(excinfo.value)


def test_resolve_auth_context_raises_naming_issuer_when_no_recognized_algorithm_advertised() -> (
    None
):
    transport = _ScriptedDiscoveryTransport(
        {
            "jwks_uri": f"{_ISSUER}/jwks.json",
            "id_token_signing_alg_values_supported": ["HS256"],
        }
    )

    with pytest.raises(AuthDiscoveryError) as excinfo:
        resolve_auth_context(
            _config(auth_issuer=_ISSUER, auth_audience=_AUDIENCE),
            transport=transport,
        )

    assert _ISSUER in str(excinfo.value)


def test_resolve_auth_context_allow_list_excludes_symmetric_and_none_from_mixed_list() -> None:
    transport = _ScriptedDiscoveryTransport(
        {
            "jwks_uri": f"{_ISSUER}/jwks.json",
            "id_token_signing_alg_values_supported": [
                "RS256",
                "HS256",
                "ES384",
                "none",
                "PS512",
            ],
        }
    )

    context = resolve_auth_context(
        _config(auth_issuer=_ISSUER, auth_audience=_AUDIENCE),
        transport=transport,
    )

    assert context is not None
    assert context.allowed_algorithms == frozenset({"RS256", "ES384", "PS512"})
