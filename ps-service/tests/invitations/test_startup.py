"""AC-BI-003/AC-BI-004: PS Service refuses to start when the Authentik service
credential (`PS_AUTHENTIK_API_TOKEN`/`PS_AUTHENTIK_BASE_URL`) is not configured
(issue #140, CHANGES.md Row 2/Appendix B).

Unlike `tests/authz/test_bootstrap_owner_startup_fail_closed.py`'s
`require_bootstrap_owner_configured`, this check is deliberately
**unconditional**: AC-BI-003's wording carries no local-test-bypass carve-out
(unlike AC-BI-002's), so `require_authentik_credential_configured` raises
regardless of `is_local_test_bypass_active` -- every non-bypass test here
(and the one bypass=True test) must supply both fields to reach `create_app`
without raising.

`require_authentik_credential_configured` is wired into `create_app`
immediately after `require_bootstrap_owner_configured`, so every test here
must also satisfy `resolve_auth_context`'s and `require_bootstrap_owner_configured`'s
own requirements when not using the bypass, mirroring
`test_bootstrap_owner_startup_fail_closed.py`'s own technique exactly
(`_stub_successful_discovery` avoids a real OIDC discovery network call).
"""

from __future__ import annotations

from typing import Any

import pytest

from ps_service.config import ServiceConfig
from ps_service.invitations.errors import AuthentikCredentialConfigurationError
from ps_service.main import create_app

_ISSUER = "https://issuer.example.com"
_AUTHENTIK_API_TOKEN = "test-authentik-token"
_AUTHENTIK_BASE_URL = "https://authentik.example.com"


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


def _stub_successful_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `resolve_auth_context` succeed without network access.

    Mirrors `test_bootstrap_owner_startup_fail_closed.py`'s own helper of the
    same name verbatim.
    """

    def _fake_fetch_discovery_document(issuer: str, **_kwargs: object) -> dict[str, Any]:
        return {
            "jwks_uri": f"{issuer}/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    monkeypatch.setattr(
        "ps_service.auth.startup.fetch_discovery_document", _fake_fetch_discovery_document
    )


def _fully_configured(**overrides: object) -> ServiceConfig:
    """A `ServiceConfig` satisfying `resolve_auth_context` and
    `require_bootstrap_owner_configured`, so only the Authentik credential
    check under test is left to trip (or not).
    """
    return _config(
        auth_issuer=_ISSUER,
        auth_audience="ps-service",
        authz_bootstrap_owner_subject="first-owner-subject",
        authz_bootstrap_owner_issuer=_ISSUER,
        **overrides,
    )


def test_create_app_raises_naming_both_vars_when_authentik_credential_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_discovery(monkeypatch)

    with pytest.raises(AuthentikCredentialConfigurationError) as excinfo:
        create_app(_fully_configured())

    message = str(excinfo.value)
    assert "authentik_api_token" in message
    assert "authentik_base_url" in message


def test_create_app_raises_naming_only_base_url_when_token_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_discovery(monkeypatch)

    with pytest.raises(AuthentikCredentialConfigurationError) as excinfo:
        create_app(_fully_configured(authentik_api_token=_AUTHENTIK_API_TOKEN))

    message = str(excinfo.value)
    assert "authentik_base_url" in message
    assert "authentik_api_token" not in message


def test_create_app_raises_naming_only_token_when_base_url_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_discovery(monkeypatch)

    with pytest.raises(AuthentikCredentialConfigurationError) as excinfo:
        create_app(_fully_configured(authentik_base_url=_AUTHENTIK_BASE_URL))

    message = str(excinfo.value)
    assert "authentik_api_token" in message
    assert "authentik_base_url" not in message


def test_create_app_does_not_raise_when_authentik_credential_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_discovery(monkeypatch)

    app = create_app(
        _fully_configured(
            authentik_api_token=_AUTHENTIK_API_TOKEN,
            authentik_base_url=_AUTHENTIK_BASE_URL,
        )
    )

    assert app is not None


def test_create_app_raises_even_when_local_test_bypass_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-003 carries no bypass carve-out (CHANGES.md Row 2): unlike
    `require_bootstrap_owner_configured`, the bypass being active must not
    exempt the Authentik credential check.
    """
    with pytest.raises(AuthentikCredentialConfigurationError):
        create_app(_config(is_local_test_bypass_active=True))


def test_create_app_does_not_raise_when_bypass_active_and_authentik_credential_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        _config(
            is_local_test_bypass_active=True,
            authentik_api_token=_AUTHENTIK_API_TOKEN,
            authentik_base_url=_AUTHENTIK_BASE_URL,
        )
    )

    assert app is not None
