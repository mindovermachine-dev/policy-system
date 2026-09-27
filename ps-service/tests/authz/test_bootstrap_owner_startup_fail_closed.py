"""AC-BI-001/AC-BI-002: PS Service refuses to start when the RBAC bootstrap owner identity
(subject + issuer) is not configured, mirroring `tests/auth/test_startup_fail_closed.py`'s
`AuthConfigurationError` presence-check shape exactly (issue #144, PLAN.md Slice 1).

Only the fail-closed *presence* check is covered here -- `bootstrap_first_owner`'s own
match/no-match comparison logic (AC-BI-003/AC-BI-004) is Slice 2/3's concern, exercised
against `FakeAccessRoleStore`, not this file.

`require_bootstrap_owner_configured` is wired into `create_app` immediately after
`resolve_auth_context`, so every non-bypass test here must also satisfy
`resolve_auth_context`'s own requirement (`auth_issuer`/`auth_audience` set) or that
earlier gate raises `AuthConfigurationError` first and masks the assertion under test --
same technique CHANGES.md's Helm-guard tests use, applied at the Python layer. Since
`auth_issuer`/`auth_audience` being set makes `resolve_auth_context` perform a real OIDC
discovery fetch, `ps_service.auth.startup.fetch_discovery_document` is monkeypatched to a
stub that succeeds without any network access, isolating these tests to the bootstrap-owner
gate under test.
"""

from __future__ import annotations

from typing import Any

import pytest

from ps_service.authz.errors import AccessRoleBootstrapConfigurationError
from ps_service.config import ServiceConfig
from ps_service.main import create_app

_ISSUER = "https://issuer.example.com"


def _config(**overrides: object) -> ServiceConfig:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "is_local_test_bypass_active": False,
        # Unconditional (issue #140): `require_authentik_credential_configured`
        # carries no bypass exemption, so every test in this file needs both
        # set too -- including `test_create_app_does_not_raise_when_bypass_
        # active_even_with_bootstrap_owner_unset` below.
        "authentik_api_token": "test-authentik-token",
        "authentik_base_url": "https://authentik.example.com",
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


def _stub_successful_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `resolve_auth_context` succeed without network access, per this module's docstring."""

    def _fake_fetch_discovery_document(issuer: str, **_kwargs: object) -> dict[str, Any]:
        return {
            "jwks_uri": f"{issuer}/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256"],
        }

    monkeypatch.setattr(
        "ps_service.auth.startup.fetch_discovery_document", _fake_fetch_discovery_document
    )


def test_create_app_raises_naming_both_vars_when_bootstrap_owner_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_discovery(monkeypatch)

    with pytest.raises(AccessRoleBootstrapConfigurationError) as excinfo:
        create_app(_config(auth_issuer=_ISSUER, auth_audience="ps-service"))

    message = str(excinfo.value)
    assert (
        "PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT and PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER are unset" in message
    )
    assert "PS_SERVICE_LOCAL_TEST_BYPASS" in message


def test_create_app_raises_naming_only_issuer_when_subject_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_discovery(monkeypatch)

    with pytest.raises(AccessRoleBootstrapConfigurationError) as excinfo:
        create_app(
            _config(
                auth_issuer=_ISSUER,
                auth_audience="ps-service",
                authz_bootstrap_owner_subject="first-owner-subject",
            )
        )

    message = str(excinfo.value)
    assert "PS_AUTHZ_BOOTSTRAP_OWNER_ISSUER is unset" in message
    assert "PS_SERVICE_LOCAL_TEST_BYPASS" in message


def test_create_app_raises_naming_only_subject_when_issuer_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_discovery(monkeypatch)

    with pytest.raises(AccessRoleBootstrapConfigurationError) as excinfo:
        create_app(
            _config(
                auth_issuer=_ISSUER,
                auth_audience="ps-service",
                authz_bootstrap_owner_issuer=f"{_ISSUER}/",
            )
        )

    message = str(excinfo.value)
    assert "PS_AUTHZ_BOOTSTRAP_OWNER_SUBJECT is unset" in message
    assert "PS_SERVICE_LOCAL_TEST_BYPASS" in message


def test_create_app_does_not_raise_when_bootstrap_owner_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_discovery(monkeypatch)

    app = create_app(
        _config(
            auth_issuer=_ISSUER,
            auth_audience="ps-service",
            authz_bootstrap_owner_subject="first-owner-subject",
            authz_bootstrap_owner_issuer=f"{_ISSUER}/",
        )
    )

    assert app is not None


def test_create_app_does_not_raise_when_bypass_active_even_with_bootstrap_owner_unset() -> None:
    app = create_app(_config(is_local_test_bypass_active=True))

    assert app is not None
