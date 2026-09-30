"""HTTP tests for `RestAuthMiddleware` -- the REST-side 401 path (issue #58, Slice 3).

AC-BI-003: a protected-route request with no/malformed `Authorization` header,
or a token failing signature/`iss`/`aud`/`exp`/`nbf`, gets 401 with
`WWW-Authenticate: Bearer resource_metadata="..."` and the handler is never
invoked. AC-BI-004: the 401 body contains neither the presented token nor
validation internals (key ids, library errors).

Drives the real composition root (`create_app`) against
`tests.auth.mock_oidc_provider`'s real local HTTP server: `resolve_auth_context`
performs a genuine OIDC-discovery fetch against it (`ps_service.main.create_app`
never overrides `resolve_auth_context`'s default `transport`), and
`PsTokenVerifier` performs a genuine JWKS fetch on first use -- no
monkeypatched transport anywhere in this file (AC-BI-017). `GET /catalog`
(an existing, already-unauthenticated-before-this-issue route,
`ps_service/api/routes.py:378`) is the protected route under test throughout.

Issue #163 remediation (Slice K): `GET /catalog`'s real, unpatched
`ps_service.api.routes.list_curated_catalog` handler runs in every test
below -- never replaced by an `AsyncMock`/spy. "Handler reached or not" is
now observed as *state* on a hand-built fake HTTP transport
(`api._fakes.FakeCuratedSourceTransport`, wired in via the same
`app.dependency_overrides[provide_curated_catalog_dependencies]` seam
`test_routes_catalog.py` already uses): a 401 case asserts
`transport.requests == []` (the real `fetch_catalog` -- and thus the real
handler -- never ran), and the one 200 case asserts on the real HTTP
response body instead of a spy's return value. The positive-detection
"which `Principal` did the handler actually receive" tests
(`_client_with_principal_capturing_handler`/`_PrincipalCapturingOverride`)
keep observing that value, but now do so by wrapping the *real*
`ps_service.api.dependencies.get_principal` via
`app.dependency_overrides[get_principal]` (the same DI-override seam
FastAPI already supports for any `Depends(...)`-declared parameter) --
calling through to the real collaborator and recording what it returned,
rather than replacing the whole route handler with a substitute.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import Request  # noqa: TC002 -- FastAPI resolves this annotation at runtime
from fastapi.testclient import TestClient

from api._fakes import FakeCuratedSourceTransport, build_fake_curated_catalog_dependencies
from ps_service.api.dependencies import get_principal, provide_curated_catalog_dependencies
from ps_service.auth import Principal
from ps_service.config import ServiceConfig
from ps_service.main import create_app

# `mock_oidc_provider_fixture` registers pytest's "mock_oidc_provider" fixture
# (see `tests.auth.mock_oidc_provider`'s module docstring for why it is
# imported under this name, not `mock_oidc_provider` itself, which every test
# below declares as a same-named parameter instead) -- never called directly.
from ps_test_support.mock_oidc_provider import (
    MockOidcProvider,
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from pathlib import Path

_AUDIENCE = "ps-service"


@pytest.fixture(autouse=True)
def _configure_logging_for_auth_tests(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    configured_logging: Path,
) -> None:
    """Slice 10: `PsTokenVerifier.verify_token` now always logs (AC-BI-014/015)
    on every request in this file -- install a real process-wide Logging
    facade (`tests/api/conftest.py`'s own `configured_logging` fixture) so
    `emit_log_entry`'s no-default-configured guard never trips here. No test
    in this file asserts on log content; that is `test_audit_logging.py`'s
    job (`tests/auth/`).
    """


def _config(provider: MockOidcProvider, **overrides: object) -> ServiceConfig:
    defaults: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 8000,
        "graceful_shutdown_seconds": 10,
        "logging_dir": None,
        "is_local_test_bypass_active": False,
        "auth_issuer": provider.issuer,
        "auth_audience": _AUDIENCE,
        "authz_bootstrap_owner_subject": "first-owner-subject",
        "authz_bootstrap_owner_issuer": provider.issuer,
        "authentik_api_token": "test-authentik-token",
        "authentik_base_url": "https://authentik.example.com",
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


def _client_with_fake_catalog_transport(
    provider: MockOidcProvider, **config_overrides: object
) -> tuple[TestClient, FakeCuratedSourceTransport]:
    """Build a `TestClient` whose real `GET /catalog` handler fetches through a fake transport.

    `list_curated_catalog` (`ps_service/api/routes.py`) is never replaced --
    only its `CuratedCatalogDependencies` (`Depends(provide_curated_catalog_dependencies)`)
    is overridden, the same `app.dependency_overrides` seam
    `test_routes_catalog.py` already uses. "Was the handler reached" is now a
    state question answerable off `transport.requests` (empty -- never
    reached -- for every 401 case below), not an interaction count on a
    substitute standing in for the handler.
    """
    transport = FakeCuratedSourceTransport(b"[]")
    app = create_app(_config(provider, **config_overrides))
    app.dependency_overrides[provide_curated_catalog_dependencies] = lambda: (
        build_fake_curated_catalog_dependencies(transport)
    )
    client = TestClient(app)
    return client, transport


class _PrincipalCapturingOverride:
    """Wraps the real `get_principal` dependency to observe what it resolved.

    `app.dependency_overrides[get_principal] = ...` is the same DI-override
    seam FastAPI already supports for any `Depends(...)`-declared parameter
    (`list_curated_catalog`'s own `principal: Annotated[Principal | None,
    Depends(get_principal)]`). This wrapper *calls* the real
    `ps_service.api.dependencies.get_principal(request)` and records what it
    returned before passing it straight through -- the real collaborator is
    still the one resolving the `Principal`; this only observes the result,
    it never substitutes for it.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.received_principal: Principal | None = None

    def __call__(self, request: Request) -> Principal | None:
        principal = get_principal(request)
        self.call_count += 1
        self.received_principal = principal
        return principal


def _client_with_principal_capturing_handler(
    provider: MockOidcProvider, **config_overrides: object
) -> tuple[TestClient, _PrincipalCapturingOverride]:
    """Build a `TestClient` whose real `GET /catalog` handler runs with an observed `get_principal`.

    Combines `_client_with_fake_catalog_transport`'s real-handler-plus-fake-
    transport wiring with a `get_principal` override
    (`_PrincipalCapturingOverride`) so the test can assert on the exact
    `Principal` FastAPI resolved for the request, without ever replacing
    `list_curated_catalog` itself.
    """
    transport = FakeCuratedSourceTransport(b"[]")
    app = create_app(_config(provider, **config_overrides))
    app.dependency_overrides[provide_curated_catalog_dependencies] = lambda: (
        build_fake_curated_catalog_dependencies(transport)
    )
    override = _PrincipalCapturingOverride()
    app.dependency_overrides[get_principal] = override
    client = TestClient(app)
    return client, override


def test_no_authorization_header_returns_401_with_www_authenticate_and_handler_never_invoked(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    client, transport = _client_with_fake_catalog_transport(mock_oidc_provider)

    response = client.get("/catalog")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="http://testserver/.well-known/oauth-protected-resource"'
    )
    body = response.json()
    assert body == {
        "error": {
            "code": "unauthenticated",
            "message": "Authentication required.",
            "failing_stage": None,
        },
        "run_id": None,
    }
    assert transport.requests == []


@pytest.mark.parametrize("header_value", ["Basic xyz", "Bearer", "NotBearer sometoken"])
def test_malformed_authorization_header_returns_401(
    mock_oidc_provider: MockOidcProvider,
    header_value: str,
) -> None:
    client, transport = _client_with_fake_catalog_transport(mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": header_value})

    assert response.status_code == 401
    assert transport.requests == []
    assert header_value not in response.text


def test_token_signed_by_an_untrusted_key_returns_401(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """A token claiming the right `iss`/`aud` but signed by a key never in this issuer's JWKS."""
    untrusted_provider = MockOidcProvider()
    try:
        token = untrusted_provider.mint_token(aud=_AUDIENCE, iss=mock_oidc_provider.issuer)
        client, transport = _client_with_fake_catalog_transport(mock_oidc_provider)

        response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})
    finally:
        untrusted_provider.shutdown()

    assert response.status_code == 401
    assert transport.requests == []


def test_expired_token_returns_401(mock_oidc_provider: MockOidcProvider) -> None:
    token = mock_oidc_provider.mint_token(aud=_AUDIENCE, exp_delta=-3600)
    client, transport = _client_with_fake_catalog_transport(mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert transport.requests == []


def test_immature_token_with_future_nbf_returns_401(mock_oidc_provider: MockOidcProvider) -> None:
    token = mock_oidc_provider.mint_token(aud=_AUDIENCE, extra_claims={"nbf": 9_999_999_999})
    client, transport = _client_with_fake_catalog_transport(mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert transport.requests == []


def test_wrong_audience_token_returns_401(mock_oidc_provider: MockOidcProvider) -> None:
    token = mock_oidc_provider.mint_token(aud="some-other-audience")
    client, transport = _client_with_fake_catalog_transport(mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert transport.requests == []


def test_wrong_issuer_token_returns_401(mock_oidc_provider: MockOidcProvider) -> None:
    token = mock_oidc_provider.mint_token(
        aud=_AUDIENCE, iss="https://not-the-configured-issuer.example.com"
    )
    client, transport = _client_with_fake_catalog_transport(mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert transport.requests == []


def test_401_body_never_contains_the_raw_token_or_pyjwt_internals(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """AC-BI-004: no presented token, no key id, no library error text in any 401 body."""
    token = mock_oidc_provider.mint_token(aud="wrong-audience")
    client, transport = _client_with_fake_catalog_transport(mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert transport.requests == []
    body_text = response.text
    assert token not in body_text
    for leaking_term in (
        "InvalidAudienceError",
        "InvalidSignatureError",
        "InvalidIssuerError",
        "ExpiredSignatureError",
        "PyJWKClientError",
        "key-1",
        "kid",
    ):
        assert leaking_term not in body_text


def test_valid_token_returns_200_and_handler_receives_matching_principal(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """AC-BI-005: a token that passes validation runs the handler with a `Principal`.

    The positive-detection companion to the 401/`transport.requests == []`
    tests above: proves the real handler is dispatched exactly once
    (distinguishing "the gate correctly let this request through" from "the
    override was never wired at all") *and* that the `Principal` the real
    `get_principal` collaborator resolved (via `Depends(get_principal)`)
    carries the minted token's own `sub`/`iss` -- not a placeholder, not
    `None`.
    """
    token = mock_oidc_provider.mint_token(sub="alice@example.com", aud=_AUDIENCE)
    client, override = _client_with_principal_capturing_handler(mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"instruments": []}
    assert override.call_count == 1
    assert override.received_principal == Principal(
        sub="alice@example.com", iss=mock_oidc_provider.issuer
    )


def test_local_test_bypass_active_runs_handler_with_no_principal(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """When the local-test bypass (#67) is active, no token is required and no `Principal` is bound.

    `RestAuthMiddleware` stores `None` under `scope["ps_principal"]` in this
    mode (Slice 3, `middleware.py`'s own bypass branch) -- `get_principal`
    must surface that as `None`, not raise or fabricate one, matching #67's
    existing unauthenticated contract exactly.
    """
    client, override = _client_with_principal_capturing_handler(
        mock_oidc_provider, is_local_test_bypass_active=True
    )

    response = client.get("/catalog")

    assert response.status_code == 200
    assert override.call_count == 1
    assert override.received_principal is None
