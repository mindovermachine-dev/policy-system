"""PS Service refuses to start without a configured public origin unless local-test mode is on."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.auth.errors import AuthConfigurationError
from ps_service.config import ServiceConfig
from ps_service.main import create_app

# Registers pytest's "mock_oidc_provider" fixture; never called directly.
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from ps_test_support.mock_oidc_provider import MockOidcProvider


def _config(**overrides: object) -> ServiceConfig:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "is_local_test_bypass_active": True,
        "authentik_api_token": "test-authentik-token",
        "authentik_base_url": "https://authentik.example.com",
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


def test_create_app_starts_in_local_test_mode_without_a_public_url() -> None:
    app = create_app(_config())

    assert app.state.config.public_url is None


def test_create_app_starts_in_local_test_mode_with_a_public_url() -> None:
    app = create_app(_config(public_url="http://localhost:8000"))

    assert app.state.config.public_url == "http://localhost:8000"


def test_create_app_refuses_to_start_without_a_public_url_outside_local_test_mode(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    config = _config(
        is_local_test_bypass_active=False,
        auth_issuer=mock_oidc_provider.issuer,
        auth_audience="ps-service",
        authz_bootstrap_owner_subject="first-owner-subject",
        authz_bootstrap_owner_issuer=mock_oidc_provider.issuer,
    )

    with pytest.raises(AuthConfigurationError, match="PS_PUBLIC_URL"):
        create_app(config)


def test_create_app_starts_outside_local_test_mode_with_a_public_url(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    config = _config(
        is_local_test_bypass_active=False,
        auth_issuer=mock_oidc_provider.issuer,
        auth_audience="ps-service",
        authz_bootstrap_owner_subject="first-owner-subject",
        authz_bootstrap_owner_issuer=mock_oidc_provider.issuer,
        public_url="https://ps.example.com",
    )

    app = create_app(config)

    assert app.state.config.public_url == "https://ps.example.com"
