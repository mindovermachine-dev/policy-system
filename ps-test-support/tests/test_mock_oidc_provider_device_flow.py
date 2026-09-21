"""Proves `MockOidcProvider`'s own device-authorization + token endpoint simulation (issue #57).

Red-before-green for Slice 4: none of this behavior exists before this
slice (the endpoints 404, `discovery_document` lacks the two new fields).
Drives the real local HTTP server the provider runs -- no monkeypatching --
via plain `urllib.request` POSTs, mirroring how `ps-cli`'s real device-flow
client will talk to it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from ps_test_support.mock_oidc_provider import MockOidcProvider


def _post_form(url: str, data: dict[str, str]) -> tuple[int, dict[str, object]]:
    """POST form-encoded `data` to `url`, returning `(status_code, json_body)` either way."""
    body = urlencode(data).encode("utf-8")
    request = Request(url, data=body, method="POST")  # noqa: S310 -- loopback test server, not a real network call
    try:
        with urlopen(request) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _request_device_authorization(
    provider: MockOidcProvider, *, client_id: str = "test-client", audience: str | None = None
) -> dict[str, object]:
    data = {"client_id": client_id, "scope": "openid"}
    if audience is not None:
        data["audience"] = audience
    status, body = _post_form(f"{provider.base_url}/device_authorization", data)
    assert status == 200
    return body


def _poll_token(
    provider: MockOidcProvider, device_code: str, *, client_id: str = "test-client"
) -> tuple[int, dict[str, object]]:
    return _post_form(
        f"{provider.base_url}/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client_id,
        },
    )


def test_discovery_document_advertises_device_and_token_endpoints(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    document = mock_oidc_provider.discovery_document()
    base_url = mock_oidc_provider.base_url
    assert document["device_authorization_endpoint"] == f"{base_url}/device_authorization"
    assert document["token_endpoint"] == f"{base_url}/token"
    # additive only -- Slice 3's fields are untouched
    assert document["issuer"] == mock_oidc_provider.issuer
    assert document["jwks_uri"] == mock_oidc_provider.jwks_uri


def test_device_authorization_response_shape(mock_oidc_provider: MockOidcProvider) -> None:
    body = _request_device_authorization(mock_oidc_provider)
    assert set(body) == {
        "device_code",
        "user_code",
        "verification_uri",
        "verification_uri_complete",
        "expires_in",
        "interval",
    }
    assert body["expires_in"] == 600
    assert body["interval"] == 1


def test_pending_poll_returns_authorization_pending(mock_oidc_provider: MockOidcProvider) -> None:
    body = _request_device_authorization(mock_oidc_provider)
    status, response = _poll_token(mock_oidc_provider, str(body["device_code"]))
    assert status == 400
    assert response == {"error": "authorization_pending"}


def test_complete_device_flow_then_poll_succeeds(mock_oidc_provider: MockOidcProvider) -> None:
    body = _request_device_authorization(mock_oidc_provider)
    device_code = str(body["device_code"])

    mock_oidc_provider.complete_device_flow(device_code, sub="alice")
    status, response = _poll_token(mock_oidc_provider, device_code)

    assert status == 200
    assert response["token_type"] == "Bearer"
    assert response["expires_in"] == 3600
    assert isinstance(response["access_token"], str)
    assert isinstance(response["refresh_token"], str)


def test_simulate_slow_down_once_returns_slow_down_exactly_once_then_reverts(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    body = _request_device_authorization(mock_oidc_provider)
    device_code = str(body["device_code"])

    mock_oidc_provider.simulate_slow_down_once(device_code)

    status_1, response_1 = _poll_token(mock_oidc_provider, device_code)
    assert status_1 == 400
    assert response_1 == {"error": "slow_down"}

    # Reverts to the underlying (still-pending) status on the next poll.
    status_2, response_2 = _poll_token(mock_oidc_provider, device_code)
    assert status_2 == 400
    assert response_2 == {"error": "authorization_pending"}


def test_expire_device_code_returns_expired_token(mock_oidc_provider: MockOidcProvider) -> None:
    body = _request_device_authorization(mock_oidc_provider)
    device_code = str(body["device_code"])

    mock_oidc_provider.expire_device_code(device_code)
    status, response = _poll_token(mock_oidc_provider, device_code)

    assert status == 400
    assert response == {"error": "expired_token"}


def test_deny_device_code_returns_access_denied(mock_oidc_provider: MockOidcProvider) -> None:
    body = _request_device_authorization(mock_oidc_provider)
    device_code = str(body["device_code"])

    mock_oidc_provider.deny_device_code(device_code)
    status, response = _poll_token(mock_oidc_provider, device_code)

    assert status == 400
    assert response == {"error": "access_denied"}


def test_audience_is_recorded_and_readable_back(mock_oidc_provider: MockOidcProvider) -> None:
    body = _request_device_authorization(
        mock_oidc_provider, client_id="ps-cli", audience="ps-service"
    )
    device_code = str(body["device_code"])

    state = mock_oidc_provider.device_flow_state(device_code)

    assert state.client_id == "ps-cli"
    assert state.audience == "ps-service"


def test_refresh_grant_rotates_the_refresh_token_and_rejects_the_old_one_on_reuse(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    body = _request_device_authorization(mock_oidc_provider, client_id="ps-cli")
    device_code = str(body["device_code"])
    mock_oidc_provider.complete_device_flow(device_code)

    _, first_token_response = _poll_token(mock_oidc_provider, device_code, client_id="ps-cli")
    first_refresh_token = str(first_token_response["refresh_token"])

    status, refreshed = _post_form(
        f"{mock_oidc_provider.base_url}/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": first_refresh_token,
            "client_id": "ps-cli",
        },
    )
    assert status == 200
    second_refresh_token = str(refreshed["refresh_token"])
    assert second_refresh_token != first_refresh_token
    assert isinstance(refreshed["access_token"], str)

    # The rotated-away first refresh token is now rejected.
    status_reuse, rejected = _post_form(
        f"{mock_oidc_provider.base_url}/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": first_refresh_token,
            "client_id": "ps-cli",
        },
    )
    assert status_reuse == 400
    assert rejected == {"error": "invalid_grant"}

    # The current (rotated-to) refresh token still works.
    status_current, _ = _post_form(
        f"{mock_oidc_provider.base_url}/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": second_refresh_token,
            "client_id": "ps-cli",
        },
    )
    assert status_current == 200
