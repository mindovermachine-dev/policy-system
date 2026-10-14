"""Tests for `scripts/grant_access_role_client.py::resolve_bearer_token`.

The helper serves `scripts/grant-access-role.sh`. It is loaded by file path, the same way
`ps-service/tests/scripts/test_check_mock_boundaries.py` loads its script. The credential
store is a real `PersistenceCredentialStore` over the hand-written in-memory persistence
fake (`conftest.build_in_memory_persistence`), and token refresh runs against the real
`MockOidcProvider`; nothing is patched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli.credentials import PersistenceCredentialStore, TokenBundle
from ps_cli.device_flow import poll_for_token, request_device_authorization
from ps_cli.errors import PsCliError
from ps_cli.oidc_discovery import ResolvedAuthParameters
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from conftest import InMemoryPersistenceBackend

    from ps_test_support.mock_oidc_provider import MockOidcProvider

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLIENT_ID = "ps-cli-test-client"
_CONTEXT = "dev"
_SERVICE_URL = "http://ps-service.example"


def _load_helper() -> ModuleType:
    """Import `scripts/grant_access_role_client.py` by path (it is not a package member)."""
    path = _REPO_ROOT / "scripts" / "grant_access_role_client.py"
    spec = importlib.util.spec_from_file_location("grant_access_role_client", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["grant_access_role_client"] = module
    spec.loader.exec_module(module)
    return module


def _real_refresh_token(provider: MockOidcProvider) -> str:
    """Run a real device-flow login against `provider`; return the refresh token it minted."""
    params = ResolvedAuthParameters(
        issuer=provider.issuer,
        client_id=_CLIENT_ID,
        scopes=("openid",),
        audience=None,
        device_authorization_endpoint=f"{provider.base_url}/device_authorization",
        token_endpoint=f"{provider.base_url}/token",
    )
    device_auth = request_device_authorization(params)
    provider.complete_device_flow(device_auth.device_code)
    refresh_token = poll_for_token(params, device_auth, sleep=lambda _: None).refresh_token
    assert refresh_token is not None
    return refresh_token


def _transport_for(provider: MockOidcProvider) -> httpx.BaseTransport:
    """Resource metadata from a fake PS Service endpoint; every other request goes real."""

    def _handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/oauth-protected-resource"
        return httpx.Response(
            200,
            json={
                "resource": _SERVICE_URL,
                "authorization_servers": [provider.issuer],
                "scopes_supported": ["openid"],
                "ps_cli_client_id": _CLIENT_ID,
            },
        )

    class _Split(httpx.BaseTransport):
        def __init__(self) -> None:
            self._metadata = httpx.MockTransport(_handle)
            self._real = httpx.HTTPTransport()

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/oauth-protected-resource":
                return self._metadata.handle_request(request)
            return self._real.handle_request(request)

    return _Split()


def _store(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
) -> PersistenceCredentialStore:
    return PersistenceCredentialStore(build_persistence=build_in_memory_persistence)


def test_explicit_token_env_wins_and_the_store_is_never_read(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.setenv(helper.TOKEN_ENV, "explicit-token")

    token = helper.resolve_bearer_token(
        context_name=_CONTEXT,
        service_url=_SERVICE_URL,
        auth_override=None,
        credential_store=_store(build_in_memory_persistence),
    )

    assert token == "explicit-token"
    assert build_in_memory_persistence(_CONTEXT).loads == 0


def test_no_context_yields_no_token(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.delenv(helper.TOKEN_ENV, raising=False)

    token = helper.resolve_bearer_token(
        context_name=None,
        service_url=_SERVICE_URL,
        auth_override=None,
        credential_store=_store(build_in_memory_persistence),
    )

    assert token is None


def test_named_context_with_nothing_stored_yields_no_token(
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.delenv(helper.TOKEN_ENV, raising=False)

    token = helper.resolve_bearer_token(
        context_name=_CONTEXT,
        service_url=_SERVICE_URL,
        auth_override=None,
        credential_store=_store(build_in_memory_persistence),
    )

    assert token is None


def test_stored_credential_with_a_rejected_refresh_token_raises_instead_of_downgrading(
    mock_oidc_provider: MockOidcProvider,
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.delenv(helper.TOKEN_ENV, raising=False)
    store = _store(build_in_memory_persistence)
    store.set_tokens(
        _CONTEXT, TokenBundle(refresh_token="bogus-refresh-token", issuer=mock_oidc_provider.issuer)
    )

    with pytest.raises(PsCliError):
        helper.resolve_bearer_token(
            context_name=_CONTEXT,
            service_url=_SERVICE_URL,
            auth_override=None,
            credential_store=store,
            transport=_transport_for(mock_oidc_provider),
        )


def test_stored_refresh_token_yields_an_access_token_and_persists_the_rotated_refresh_token(
    mock_oidc_provider: MockOidcProvider,
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()
    monkeypatch.delenv(helper.TOKEN_ENV, raising=False)
    store = _store(build_in_memory_persistence)
    original = _real_refresh_token(mock_oidc_provider)
    store.set_tokens(
        _CONTEXT, TokenBundle(refresh_token=original, issuer=mock_oidc_provider.issuer)
    )

    token = helper.resolve_bearer_token(
        context_name=_CONTEXT,
        service_url=_SERVICE_URL,
        auth_override=None,
        credential_store=store,
        transport=_transport_for(mock_oidc_provider),
    )

    assert isinstance(token, str)
    assert token
    persisted = store.get_tokens(_CONTEXT)
    assert persisted is not None
    assert persisted.refresh_token != original
