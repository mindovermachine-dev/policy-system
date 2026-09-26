"""Tests for ps_cli.oidc_discovery (issue #57 Slices 5-6).

Slice 5: `fetch_protected_resource_metadata()`/`resolve_client_id()` against a fake
PS-Service resource-metadata endpoint -- an `httpx.MockTransport`, not the real
`MockOidcProvider` (this is PS Service's own endpoint, not the IdP's), mirroring
`test_http_client.py`'s own testing convention for PS-Service-side endpoints.

Slice 6: `fetch_openid_configuration()`/`resolve_auth_parameters()` against the real
`ps_test_support.mock_oidc_provider.MockOidcProvider` -- this *is* the IdP's own
endpoint, the one D-57-3 exists to let both packages test against consistently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli.errors import PsCliError
from ps_cli.oidc_discovery import (
    OidcDiscoveryDocument,
    ProtectedResourceMetadata,
    ResolvedAuthParameters,
    _assert_secure_or_loopback,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, per its own AC-BI-017
    fetch_openid_configuration,
    fetch_protected_resource_metadata,
    resolve_auth_parameters,
    resolve_client_id,
)
from ps_cli.targets import AuthOverrides
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from ps_test_support.mock_oidc_provider import MockOidcProvider

_SERVICE_URL = "http://ps-service.example"

_RESOURCE_METADATA_BODY = {
    "resource": "http://ps-service.example",
    "authorization_servers": ["https://issuer.example"],
    "scopes_supported": ["openid", "profile"],
    "ps_cli_client_id": "cli-client-id",
}

_NO_OVERRIDE = AuthOverrides(issuer=None, client_id=None, scopes=None, audience=None)


def _handler_for(body: object, *, status: int = 200) -> httpx.MockTransport:
    def _handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/oauth-protected-resource"
        return httpx.Response(status, json=body)

    return httpx.MockTransport(_handle)


def _connect_error_transport() -> httpx.MockTransport:
    def _handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(_handle)


def _read_timeout_transport() -> httpx.MockTransport:
    def _handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    return httpx.MockTransport(_handle)


# --- Slice 5: fetch_protected_resource_metadata() ---------------------------------


def test_fetch_protected_resource_metadata_parses_happy_path_body() -> None:
    """A well-formed 200 body parses into `ProtectedResourceMetadata` field-for-field."""
    transport = _handler_for(_RESOURCE_METADATA_BODY)

    metadata = fetch_protected_resource_metadata(_SERVICE_URL, transport=transport)

    assert metadata == ProtectedResourceMetadata(
        resource="http://ps-service.example",
        authorization_servers=["https://issuer.example"],
        scopes_supported=["openid", "profile"],
        ps_cli_client_id="cli-client-id",
    )


def test_fetch_protected_resource_metadata_omits_client_id_field_when_absent() -> None:
    """`ps_cli_client_id` parses to `None` when PS Service omits the field entirely."""
    body = {k: v for k, v in _RESOURCE_METADATA_BODY.items() if k != "ps_cli_client_id"}
    transport = _handler_for(body)

    metadata = fetch_protected_resource_metadata(_SERVICE_URL, transport=transport)

    assert metadata.ps_cli_client_id is None


def test_fetch_protected_resource_metadata_connect_error_raises_ps_cli_error() -> None:
    """An unreachable PS Service raises `PsCliError`, not a raw `httpx` exception."""
    with pytest.raises(PsCliError):
        fetch_protected_resource_metadata(_SERVICE_URL, transport=_connect_error_transport())


def test_fetch_protected_resource_metadata_read_timeout_raises_ps_cli_error() -> None:
    """A read timeout raises `PsCliError`, not a raw `httpx` exception."""
    with pytest.raises(PsCliError):
        fetch_protected_resource_metadata(_SERVICE_URL, transport=_read_timeout_transport())


def test_fetch_protected_resource_metadata_non_2xx_raises_ps_cli_error() -> None:
    """A non-2xx status raises `PsCliError`."""
    transport = _handler_for({"detail": "not found"}, status=404)

    with pytest.raises(PsCliError):
        fetch_protected_resource_metadata(_SERVICE_URL, transport=transport)


def test_fetch_protected_resource_metadata_malformed_body_raises_ps_cli_error() -> None:
    """A 200 body missing required fields raises `PsCliError`, not a `KeyError`."""
    transport = _handler_for({"resource": "http://ps-service.example"})

    with pytest.raises(PsCliError):
        fetch_protected_resource_metadata(_SERVICE_URL, transport=transport)


# --- Slice 5: resolve_client_id() --------------------------------------------------


def test_resolve_client_id_uses_metadata_value_when_no_override() -> None:
    """No override -> `metadata.ps_cli_client_id` is used."""
    metadata = ProtectedResourceMetadata(
        resource="r", authorization_servers=[], scopes_supported=[], ps_cli_client_id="from-meta"
    )

    assert resolve_client_id(metadata, None) == "from-meta"


def test_resolve_client_id_override_wins_even_when_metadata_also_has_a_value() -> None:
    """`override.client_id` wins over `metadata.ps_cli_client_id` when both are set."""
    metadata = ProtectedResourceMetadata(
        resource="r", authorization_servers=[], scopes_supported=[], ps_cli_client_id="from-meta"
    )
    override = AuthOverrides(issuer=None, client_id="from-override", scopes=None, audience=None)

    assert resolve_client_id(metadata, override) == "from-override"


def test_resolve_client_id_raises_naming_both_places_when_neither_is_set() -> None:
    """Neither override nor metadata has a client id -> `PsCliError` naming both places."""
    metadata = ProtectedResourceMetadata(
        resource="r", authorization_servers=[], scopes_supported=[], ps_cli_client_id=None
    )

    with pytest.raises(PsCliError) as excinfo:
        resolve_client_id(metadata, _NO_OVERRIDE)

    assert "the resource metadata" in excinfo.value.msg
    assert "auth.client_id in targets.toml" in excinfo.value.msg


# --- Slice 6: fetch_openid_configuration() (real MockOidcProvider) ----------------


def test_fetch_openid_configuration_parses_real_provider_discovery_document(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """A real `MockOidcProvider`'s discovery document parses into `OidcDiscoveryDocument`."""
    document = fetch_openid_configuration(mock_oidc_provider.issuer)

    assert document == OidcDiscoveryDocument(
        issuer=mock_oidc_provider.issuer,
        device_authorization_endpoint=f"{mock_oidc_provider.base_url}/device_authorization",
        token_endpoint=f"{mock_oidc_provider.base_url}/token",
    )


def test_fetch_openid_configuration_ignores_unknown_fields(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """Fields other than issuer/device_authorization_endpoint/token_endpoint are ignored."""
    document = fetch_openid_configuration(mock_oidc_provider.issuer)

    assert document.issuer == mock_oidc_provider.issuer


# --- Baseline fix (issue #129, discovered in IMPL_SLICE_0B.md): trailing-slash issuer ---


def test_fetch_openid_configuration_issuer_with_trailing_slash_requests_single_slash_url() -> None:
    """An issuer ending in `/` (Authentik's own per-Application issuer shape,
    `.../application/o/<slug>/`) must not produce a double-slash discovery URL.

    Before the fix, `f"{issuer}{_OPENID_CONFIGURATION_PATH}"` concatenated
    `https://auth.example.com/application/o/ps-cli/` with
    `/.well-known/openid-configuration` verbatim, producing
    `.../ps-cli//.well-known/openid-configuration` -- a path real IdPs 404 on.
    """
    requested_urls: list[str] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "issuer": "https://auth.example.com/application/o/ps-cli/",
                "device_authorization_endpoint": "https://auth.example.com/device",
                "token_endpoint": "https://auth.example.com/token",
            },
        )

    fetch_openid_configuration(
        "https://auth.example.com/application/o/ps-cli/",
        transport=httpx.MockTransport(_handle),
    )

    assert requested_urls == [
        "https://auth.example.com/application/o/ps-cli/.well-known/openid-configuration"
    ]


def test_fetch_openid_configuration_issuer_without_trailing_slash_still_works() -> None:
    """The pre-existing, no-trailing-slash case is unchanged by the normalization fix."""
    requested_urls: list[str] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "issuer": "https://auth.example.com/application/o/ps-cli",
                "device_authorization_endpoint": "https://auth.example.com/device",
                "token_endpoint": "https://auth.example.com/token",
            },
        )

    fetch_openid_configuration(
        "https://auth.example.com/application/o/ps-cli",
        transport=httpx.MockTransport(_handle),
    )

    assert requested_urls == [
        "https://auth.example.com/application/o/ps-cli/.well-known/openid-configuration"
    ]


# --- Slice 6: resolve_auth_parameters() --------------------------------------------


def _resource_metadata_handler(
    provider: MockOidcProvider, *, scopes: list[str] | None = None
) -> httpx.MockTransport:
    """A resource-metadata transport pointing `authorization_servers` at `provider`."""

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": "http://ps-service.example",
                    "authorization_servers": [provider.issuer],
                    "scopes_supported": scopes if scopes is not None else ["openid"],
                    "ps_cli_client_id": "cli-client-id",
                },
            )
        # Any other path (the openid-configuration / device_authorization / token
        # requests) goes to the real provider's own real HTTP server, not this
        # MockTransport -- resolve_auth_parameters() only ever passes `transport`
        # to the resource-metadata half in these tests; see the None below.
        msg = f"unexpected request on the resource-metadata transport: {request.url}"
        raise AssertionError(msg)

    return httpx.MockTransport(_handle)


class _SplitTransport(httpx.BaseTransport):
    """Routes PS-Service-shaped requests to a `MockTransport`, everything else real.

    `resolve_auth_parameters()` takes a single `transport` parameter shared by both
    the resource-metadata fetch (a fake PS-Service endpoint) and the openid-
    configuration fetch (the real `MockOidcProvider`'s real HTTP server) -- this
    routes by path so both fetches happen in one test without either seam leaking
    into the other.
    """

    def __init__(self, resource_metadata_transport: httpx.MockTransport) -> None:
        self._resource_metadata_transport = resource_metadata_transport
        self._real_transport = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return self._resource_metadata_transport.handle_request(request)
        return self._real_transport.handle_request(request)


def test_resolve_auth_parameters_happy_path_discovers_everything(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """No overrides -> issuer/client_id/scopes are all discovered from the real provider."""
    transport = _SplitTransport(
        _resource_metadata_handler(mock_oidc_provider, scopes=["openid", "profile"])
    )

    result = resolve_auth_parameters(_SERVICE_URL, _NO_OVERRIDE, transport=transport)

    assert result == ResolvedAuthParameters(
        issuer=mock_oidc_provider.issuer,
        client_id="cli-client-id",
        scopes=("openid", "profile", "offline_access"),
        audience=None,
        device_authorization_endpoint=f"{mock_oidc_provider.base_url}/device_authorization",
        token_endpoint=f"{mock_oidc_provider.base_url}/token",
    )


def test_resolve_auth_parameters_issuer_override_wins_and_openid_config_targets_it(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """`auth.issuer` override wins over the discovered issuer, AND the subsequent
    openid-configuration fetch targets the *overridden* issuer -- catching an
    override-applied-too-late bug where the discovered issuer would otherwise still
    be used for the second fetch.
    """
    override = AuthOverrides(
        issuer=mock_oidc_provider.issuer, client_id=None, scopes=None, audience=None
    )

    # The resource-metadata handler only ever advertises `provider.issuer` itself as
    # its authorization_servers entry, so to prove the override -- not the
    # discovered default -- is what actually drives the second fetch, point the
    # discovered authorization_servers at a bogus, unreachable issuer instead and
    # rely on the override to redirect to the real one.
    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": "http://ps-service.example",
                    "authorization_servers": ["http://bogus-issuer.invalid"],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": "cli-client-id",
                },
            )
        msg = f"unexpected request: {request.url}"
        raise AssertionError(msg)

    transport = _SplitTransport(httpx.MockTransport(_handle))

    result = resolve_auth_parameters(_SERVICE_URL, override, transport=transport)

    assert result.issuer == mock_oidc_provider.issuer
    expected_endpoint = f"{mock_oidc_provider.base_url}/device_authorization"
    assert result.device_authorization_endpoint == expected_endpoint


def test_resolve_auth_parameters_scopes_override_wins(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """`auth.scopes` override wins over `scopes_supported` from the resource metadata.

    `offline_access` is still appended on top (issue #119, AC-BI-001) -- the override
    replaces the *discovered* scope set, not this module's own refresh-capability
    guarantee.
    """
    transport = _SplitTransport(
        _resource_metadata_handler(mock_oidc_provider, scopes=["openid", "profile"])
    )
    override = AuthOverrides(issuer=None, client_id=None, scopes=("custom-scope",), audience=None)

    result = resolve_auth_parameters(_SERVICE_URL, override, transport=transport)

    assert result.scopes == ("custom-scope", "offline_access")


def test_resolve_auth_parameters_does_not_duplicate_offline_access_when_already_advertised(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """Issue #119, AC-BI-002: a server that already advertises `offline_access` in its
    `scopes_supported` doesn't get it appended a second time.
    """
    transport = _SplitTransport(
        _resource_metadata_handler(mock_oidc_provider, scopes=["openid", "offline_access"])
    )

    result = resolve_auth_parameters(_SERVICE_URL, _NO_OVERRIDE, transport=transport)

    assert result.scopes == ("openid", "offline_access")


def test_resolve_auth_parameters_does_not_duplicate_offline_access_in_an_override(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """Issue #119, AC-BI-002: an `auth.scopes` override that already lists
    `offline_access` doesn't get it appended a second time either.
    """
    transport = _SplitTransport(_resource_metadata_handler(mock_oidc_provider, scopes=["openid"]))
    override = AuthOverrides(
        issuer=None, client_id=None, scopes=("custom-scope", "offline_access"), audience=None
    )

    result = resolve_auth_parameters(_SERVICE_URL, override, transport=transport)

    assert result.scopes == ("custom-scope", "offline_access")


def test_resolve_auth_parameters_empty_authorization_servers_with_no_override_raises() -> None:
    """Empty `authorization_servers`, no issuer override -> `PsCliError`, not `IndexError`."""

    def _handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.well-known/oauth-protected-resource"
        return httpx.Response(
            200,
            json={
                "resource": "http://ps-service.example",
                "authorization_servers": [],
                "scopes_supported": [],
                "ps_cli_client_id": "cli-client-id",
            },
        )

    with pytest.raises(PsCliError):
        resolve_auth_parameters(_SERVICE_URL, _NO_OVERRIDE, transport=httpx.MockTransport(_handle))


def test_resolve_auth_parameters_missing_device_authorization_endpoint_raises_naming_issuer(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """An issuer whose discovery document has no `device_authorization_endpoint` ->
    `PsCliError` naming the issuer (AC-BI-005).
    """
    _ = mock_oidc_provider  # unused: this issuer's own discovery doc is bypassed below

    def _resource_metadata(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": "http://ps-service.example",
                    "authorization_servers": ["http://no-device-flow.example"],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": "cli-client-id",
                },
            )
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={"issuer": "http://no-device-flow.example"},
            )
        msg = f"unexpected request: {request.url}"
        raise AssertionError(msg)

    with pytest.raises(PsCliError) as excinfo:
        resolve_auth_parameters(
            _SERVICE_URL, _NO_OVERRIDE, transport=httpx.MockTransport(_resource_metadata)
        )

    assert "http://no-device-flow.example" in excinfo.value.msg


# --- Slice 21: AC-BI-017 HTTPS-or-loopback enforcement ------------------------------


class TestAssertSecureOrLoopback:
    """Table-driven proof of `_assert_secure_or_loopback`'s own heuristic."""

    @pytest.mark.parametrize(
        ("url", "expect_error"),
        [
            ("http://evil.example.com", True),
            ("http://127.0.0.1:8000", False),
            ("http://localhost:8000", False),
            ("http://[::1]:8000", False),
            ("https://evil.example.com", False),
            ("https://127.0.0.1:8000", False),
        ],
    )
    def test_matches_expected_refusal(self, *, url: str, expect_error: bool) -> None:
        """Raise iff scheme != https AND hostname not in the loopback spelling set."""
        if expect_error:
            with pytest.raises(PsCliError) as excinfo:
                _assert_secure_or_loopback(url, what="issuer")
            assert url in excinfo.value.msg
            assert "not https" in excinfo.value.msg
        else:
            _assert_secure_or_loopback(url, what="issuer")  # must not raise


def _evil_authorization_servers_handler(*, issuer: str) -> httpx.MockTransport:
    """A resource-metadata transport naming `issuer` as the sole authorization server.

    Fails the test (`AssertionError`) if any request other than the resource-metadata
    fetch is ever made -- proving the AC-BI-017 refusal fires before the
    openid-configuration leg is ever attempted.
    """

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": "http://ps-service.example",
                    "authorization_servers": [issuer],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": "cli-client-id",
                },
            )
        msg = f"unexpected request past the AC-BI-017 issuer refusal: {request.url}"
        raise AssertionError(msg)

    return httpx.MockTransport(_handle)


def test_resolve_auth_parameters_discovered_insecure_issuer_raises_before_openid_config_fetch() -> (
    None
):
    """A *discovered* issuer of `http://evil.example.com` -> `PsCliError`, and the
    openid-configuration fetch on that issuer is never attempted (AC-BI-017).
    """
    transport = _evil_authorization_servers_handler(issuer="http://evil.example.com")

    with pytest.raises(PsCliError) as excinfo:
        resolve_auth_parameters(_SERVICE_URL, _NO_OVERRIDE, transport=transport)

    assert "http://evil.example.com" in excinfo.value.msg
    assert "not https" in excinfo.value.msg


def test_resolve_auth_parameters_overridden_insecure_issuer_raises_before_openid_config_fetch() -> (
    None
):
    """An `auth.issuer` *override* of `http://evil.example.com` -> `PsCliError`, same
    as the discovered case -- AC-BI-017's refusal applies regardless of
    discovered-vs-overridden origin. The discovered `authorization_servers` entry is
    deliberately a different, bogus host, to prove it is the *override* driving the
    refusal, not a coincidence of the discovered value.
    """
    override = AuthOverrides(
        issuer="http://evil.example.com", client_id=None, scopes=None, audience=None
    )
    transport = _evil_authorization_servers_handler(issuer="http://bogus-issuer.invalid")

    with pytest.raises(PsCliError) as excinfo:
        resolve_auth_parameters(_SERVICE_URL, override, transport=transport)

    assert "http://evil.example.com" in excinfo.value.msg
    assert "not https" in excinfo.value.msg


def test_resolve_auth_parameters_insecure_device_auth_endpoint_raises_before_device_post() -> None:
    """The issuer itself is fine (loopback), but its discovery document names an
    insecure `device_authorization_endpoint` -> `PsCliError` before Slice 9's
    device-authorization POST is ever attempted (`resolve_auth_parameters` never
    returns, so `device_flow.request_device_authorization` is never even reached).
    """
    loopback_issuer = "http://127.0.0.1:1"

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                200,
                json={
                    "resource": "http://ps-service.example",
                    "authorization_servers": [loopback_issuer],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": "cli-client-id",
                },
            )
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": loopback_issuer,
                    "device_authorization_endpoint": "http://evil.example.com/device",
                    "token_endpoint": f"{loopback_issuer}/token",
                },
            )
        msg = f"unexpected request past the AC-BI-017 endpoint refusal: {request.url}"
        raise AssertionError(msg)

    with pytest.raises(PsCliError) as excinfo:
        resolve_auth_parameters(_SERVICE_URL, _NO_OVERRIDE, transport=httpx.MockTransport(_handle))

    assert "http://evil.example.com/device" in excinfo.value.msg
    assert "not https" in excinfo.value.msg


def test_resolve_auth_parameters_loopback_issuer_and_endpoints_are_not_refused(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """A loopback `http://127.0.0.1:<port>` issuer/endpoints -- the shape every prior
    slice's tests already use -- are not refused by AC-BI-017's check. The most
    important regression check in this slice: Slices 5/6/9/10/11/12/13/14/15/16/17
    all run their own tests against loopback URLs against the real `MockOidcProvider`.
    """
    transport = _SplitTransport(_resource_metadata_handler(mock_oidc_provider))

    result = resolve_auth_parameters(_SERVICE_URL, _NO_OVERRIDE, transport=transport)

    assert result.issuer == mock_oidc_provider.issuer
    assert (
        result.device_authorization_endpoint
        == f"{mock_oidc_provider.base_url}/device_authorization"
    )
    assert result.token_endpoint == f"{mock_oidc_provider.base_url}/token"
