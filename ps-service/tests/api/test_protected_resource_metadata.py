"""HTTP tests for `GET /.well-known/oauth-protected-resource` (issue #58, Slice 8, AC-BI-010).

RFC 9728 (OAuth 2.0 Protected Resource Metadata): unauthenticated
`GET /.well-known/oauth-protected-resource` returns `resource`,
`authorization_servers: [issuer]`, `scopes_supported`, plus this project's own
`ps_cli_client_id` extension field -- present only when `PS_AUTH_CLI_CLIENT_ID`
is configured (omitted entirely, never rendered `null`, when absent).

Drives the real composition root (`create_app`) against
`tests.auth.mock_oidc_provider`'s real local HTTP server -- no monkeypatched
transport (AC-BI-017), matching `test_rest_auth_middleware.py`'s convention.
`RestAuthMiddleware`'s `_EXEMPT_PREFIX = "/.well-known/"` (Slice 3) already
covers this path, so every test below calls the route with **no**
`Authorization` header at all, proving the route is genuinely exempt rather
than merely returning a body a client happens not to need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

# `mock_oidc_provider_fixture` registers pytest's "mock_oidc_provider" fixture
# (see `tests.auth.mock_oidc_provider`'s module docstring for why it is
# imported under this name, not `mock_oidc_provider` itself, which every test
# below declares as a same-named parameter instead) -- never called directly.
from tests.auth.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    from tests.auth.mock_oidc_provider import MockOidcProvider

_AUDIENCE = "ps-service"
_PATH = "/.well-known/oauth-protected-resource"


def _config(provider: MockOidcProvider | None, **overrides: object) -> ServiceConfig:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "is_local_test_bypass_active": provider is None,
    }
    if provider is not None:
        defaults["auth_issuer"] = provider.issuer
        defaults["auth_audience"] = _AUDIENCE
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


def test_returns_200_with_rfc9728_shape_and_no_cli_client_id_when_unset(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    client = TestClient(create_app(_config(mock_oidc_provider)))

    response = client.get(_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["authorization_servers"] == [mock_oidc_provider.issuer]
    assert body["scopes_supported"] == []
    assert body["resource"].startswith("http://")
    assert "ps_cli_client_id" not in body


def test_ps_cli_client_id_present_when_configured(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    client = TestClient(
        create_app(_config(mock_oidc_provider, auth_cli_client_id="ps-cli-public-client"))
    )

    response = client.get(_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["ps_cli_client_id"] == "ps-cli-public-client"


def test_scopes_supported_reflects_configured_auth_scopes(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    client = TestClient(
        create_app(_config(mock_oidc_provider, auth_scopes=("read:catalog", "write:ingestions")))
    )

    response = client.get(_PATH)

    assert response.status_code == 200
    assert response.json()["scopes_supported"] == ["read:catalog", "write:ingestions"]


def test_reachable_with_no_authorization_header() -> None:
    """Proves the route is genuinely exempt from `RestAuthMiddleware`, not merely tolerant of it.

    Uses the local-test bypass (no `AuthContext` at all) so this test needs no
    mock provider -- the point under test is reachability with **no** header,
    not the resolved-auth-context branch (covered by the other tests above).
    """
    client = TestClient(create_app(_config(None)))

    response = client.get(_PATH)

    assert response.status_code == 200


def test_local_test_bypass_active_returns_200_with_empty_authorization_servers() -> None:
    """PLAN.md's documented degenerate case: bypass active, no `AuthContext`, still 200."""
    client = TestClient(create_app(_config(None)))

    response = client.get(_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["authorization_servers"] == []
    assert body["scopes_supported"] == []
    assert "ps_cli_client_id" not in body


def test_no_duplicate_route_registered_inside_the_mcp_mount(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """PLAN.md §0.1: `AuthSettings.resource_server_url=None` means the MCP SDK never
    auto-registers its own protected-resource-metadata route under `/mcp` --
    this is the one endpoint, reachable only at the top level.
    """
    client = TestClient(create_app(_config(mock_oidc_provider)))

    response = client.get(f"/mcp{_PATH}")

    assert response.status_code != 200
