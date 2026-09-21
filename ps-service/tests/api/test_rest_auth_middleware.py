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

CHANGES.md item 5: the `list_curated_catalog` monkeypatch is applied
*before* `create_app()`/`TestClient` construction in every test below (via
`_client_with_patched_handler`), never after -- patching afterward would be
vacuous, since `build_api_router()` binds the route to whatever function
object `ps_service.api.routes.list_curated_catalog` names at `create_app`
call time.

Slice 4 (AC-BI-005) adds the companion "valid token -> 200, handler invoked
exactly once, with the correct `Principal`" tests below, using
`_client_with_principal_capturing_handler`/`_PrincipalCapturingSpy` -- the
non-vacuous, positive-detection half CHANGES.md item 5 requires alongside
Slice 3's own `spy.call_count == 0` assertions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from ps_service.api.dependencies import get_principal
from ps_service.api.models import CuratedCatalogResponse
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
    }
    defaults.update(overrides)
    return ServiceConfig(**defaults)  # pyright: ignore[reportArgumentType]  # dict-unpacked kwargs


def _client_with_patched_handler(
    monkeypatch: pytest.MonkeyPatch,
    provider: MockOidcProvider,
    **config_overrides: object,
) -> tuple[TestClient, AsyncMock]:
    """Build a `TestClient` whose `/catalog` route handler is a call-counting spy.

    The monkeypatch is applied before `create_app(...)` runs -- see this
    file's own module docstring (CHANGES.md item 5).
    """
    spy = AsyncMock(return_value=CuratedCatalogResponse(instruments=[]))
    monkeypatch.setattr("ps_service.api.routes.list_curated_catalog", spy)
    client = TestClient(create_app(_config(provider, **config_overrides)))
    return client, spy


class _PrincipalCapturingSpy:
    """A call-counting `/catalog` handler replacement that also observes the injected `Principal`.

    A bare `AsyncMock()` (as `_client_with_patched_handler` above uses) exposes an
    `(*args, **kwargs)` signature to `inspect.signature` -- FastAPI would see no
    `Depends(get_principal)` parameter to resolve at all, so it could never receive
    the value under test. This is a real callable with a real, introspectable
    signature instead, so FastAPI's dependant-building machinery resolves
    `get_principal` and passes its result straight through -- the positive-detection
    companion to `_client_with_patched_handler`'s call-count-only spies
    (CHANGES.md item 5: "record a call and assert `spy.call_count == 1`... inspect
    the `Principal` the handler actually received").
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.received_principal: Principal | None = None

    async def __call__(
        self,
        principal: Annotated[Principal | None, Depends(get_principal)] = None,
    ) -> CuratedCatalogResponse:
        self.call_count += 1
        self.received_principal = principal
        return CuratedCatalogResponse(instruments=[])


def _client_with_principal_capturing_handler(
    monkeypatch: pytest.MonkeyPatch,
    provider: MockOidcProvider,
    **config_overrides: object,
) -> tuple[TestClient, _PrincipalCapturingSpy]:
    """Build a `TestClient` whose `/catalog` handler is a `_PrincipalCapturingSpy`.

    Applied before `create_app(...)` runs, for the same non-vacuous-patching
    reason `_client_with_patched_handler` documents (CHANGES.md item 5).
    """
    spy = _PrincipalCapturingSpy()
    monkeypatch.setattr("ps_service.api.routes.list_curated_catalog", spy)
    client = TestClient(create_app(_config(provider, **config_overrides)))
    return client, spy


def test_no_authorization_header_returns_401_with_www_authenticate_and_handler_never_invoked(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    client, spy = _client_with_patched_handler(monkeypatch, mock_oidc_provider)

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
    assert spy.call_count == 0


@pytest.mark.parametrize("header_value", ["Basic xyz", "Bearer", "NotBearer sometoken"])
def test_malformed_authorization_header_returns_401(
    monkeypatch: pytest.MonkeyPatch,
    mock_oidc_provider: MockOidcProvider,
    header_value: str,
) -> None:
    client, spy = _client_with_patched_handler(monkeypatch, mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": header_value})

    assert response.status_code == 401
    assert spy.call_count == 0
    assert header_value not in response.text


def test_token_signed_by_an_untrusted_key_returns_401(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    """A token claiming the right `iss`/`aud` but signed by a key never in this issuer's JWKS."""
    untrusted_provider = MockOidcProvider()
    try:
        token = untrusted_provider.mint_token(aud=_AUDIENCE, iss=mock_oidc_provider.issuer)
        client, spy = _client_with_patched_handler(monkeypatch, mock_oidc_provider)

        response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})
    finally:
        untrusted_provider.shutdown()

    assert response.status_code == 401
    assert spy.call_count == 0


def test_expired_token_returns_401(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    token = mock_oidc_provider.mint_token(aud=_AUDIENCE, exp_delta=-3600)
    client, spy = _client_with_patched_handler(monkeypatch, mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert spy.call_count == 0


def test_immature_token_with_future_nbf_returns_401(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    token = mock_oidc_provider.mint_token(aud=_AUDIENCE, extra_claims={"nbf": 9_999_999_999})
    client, spy = _client_with_patched_handler(monkeypatch, mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert spy.call_count == 0


def test_wrong_audience_token_returns_401(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    token = mock_oidc_provider.mint_token(aud="some-other-audience")
    client, spy = _client_with_patched_handler(monkeypatch, mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert spy.call_count == 0


def test_wrong_issuer_token_returns_401(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    token = mock_oidc_provider.mint_token(
        aud=_AUDIENCE, iss="https://not-the-configured-issuer.example.com"
    )
    client, spy = _client_with_patched_handler(monkeypatch, mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert spy.call_count == 0


def test_401_body_never_contains_the_raw_token_or_pyjwt_internals(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    """AC-BI-004: no presented token, no key id, no library error text in any 401 body."""
    token = mock_oidc_provider.mint_token(aud="wrong-audience")
    client, spy = _client_with_patched_handler(monkeypatch, mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert spy.call_count == 0
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
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    """AC-BI-005: a token that passes validation runs the handler with a `Principal`.

    The positive-detection companion to Slice 3's 401/`spy.call_count == 0`
    tests above (CHANGES.md item 5): proves the handler is dispatched exactly
    once (distinguishing "the gate correctly let this request through" from
    "the spy was never wired at all") *and* that the `Principal` the handler
    actually received (via `Depends(get_principal)`) carries the minted
    token's own `sub`/`iss` -- not a placeholder, not `None`.
    """
    token = mock_oidc_provider.mint_token(sub="alice@example.com", aud=_AUDIENCE)
    client, spy = _client_with_principal_capturing_handler(monkeypatch, mock_oidc_provider)

    response = client.get("/catalog", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"instruments": []}
    assert spy.call_count == 1
    assert spy.received_principal == Principal(
        sub="alice@example.com", iss=mock_oidc_provider.issuer
    )


def test_local_test_bypass_active_runs_handler_with_no_principal(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    """When the local-test bypass (#67) is active, no token is required and no `Principal` is bound.

    `RestAuthMiddleware` stores `None` under `scope["ps_principal"]` in this
    mode (Slice 3, `middleware.py`'s own bypass branch) -- `get_principal`
    must surface that as `None`, not raise or fabricate one, matching #67's
    existing unauthenticated contract exactly.
    """
    client, spy = _client_with_principal_capturing_handler(
        monkeypatch, mock_oidc_provider, is_local_test_bypass_active=True
    )

    response = client.get("/catalog")

    assert response.status_code == 200
    assert spy.call_count == 1
    assert spy.received_principal is None
