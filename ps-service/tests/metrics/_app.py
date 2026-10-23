"""Shared builders for the `/metrics` tests: a real `create_app` with authentication enabled."""

from __future__ import annotations

from typing import Any

from ps_service.config import ServiceConfig

AUTH_ISSUER = "https://issuer.example.com"


def fake_discovery_document(issuer: str, **_kwargs: object) -> dict[str, Any]:
    """A successful OIDC discovery document, standing in for the one real network fetch."""
    return {
        "jwks_uri": f"{issuer}/jwks.json",
        "id_token_signing_alg_values_supported": ["RS256"],
    }


def auth_enabled_config(**overrides: object) -> ServiceConfig:
    """A `ServiceConfig` with authentication on (local-test bypass off) and bootstrap set."""
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "auth_issuer": AUTH_ISSUER,
        "auth_audience": "https://api.example.com",
        "authz_bootstrap_owner_subject": "first-owner-subject",
        "authz_bootstrap_owner_issuer": AUTH_ISSUER,
        "authentik_api_token": "test-authentik-token",
        "authentik_base_url": "https://authentik.example.com",
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs
