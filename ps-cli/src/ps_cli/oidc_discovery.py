"""ps-cli OIDC discovery: PS Service resource metadata and IdP openid-configuration.

Two distinct discovery documents are fetched here, from two distinct servers, and must
not be confused:

- `ProtectedResourceMetadata` (RFC 9728) is served by PS Service itself, at
  `<service_url>/.well-known/oauth-protected-resource`. It names the authorization
  server(s) that can mint tokens PS Service accepts, plus (optionally)
  `ps_cli_client_id`, this project's own extension field. Its shape is mirrored
  field-for-field from the service's response model rather than imported, because
  `ps_cli` must never import `ps_service.*` (the decoupling rule enforced by
  `test_architecture_boundary.py`).
- `OidcDiscoveryDocument` (RFC 8414 / OIDC Discovery) is served by the IdP
  (authorization server) named in that resource metadata, at
  `<issuer>/.well-known/openid-configuration`. It names the endpoints device-flow
  login actually calls (`device_authorization_endpoint`, `token_endpoint`).

`resolve_auth_parameters()` combines the two with `AuthOverrides` (`ps_cli.targets`)
into one `ResolvedAuthParameters`: an override always wins over a discovered default,
and every case where nothing was found and there is no override fails closed with an
actionable `PsCliError` rather than an `IndexError`/`KeyError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast
from urllib.parse import urlparse

import httpx

from ps_cli.errors import CannotVerifyError, PsCliError
from ps_cli.tls import (
    CERTIFICATE_ERROR_HINT,
    build_ssl_context,
    is_certificate_verification_error,
)

if TYPE_CHECKING:
    from ps_cli.targets import AuthOverrides

_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
_OPENID_CONFIGURATION_PATH = "/.well-known/openid-configuration"

# Matches `http_client.py`'s own fast-path timeout convention (a short, uniform
# connect/read/write/pool bound) -- both discovery documents are small, static JSON
# bodies with no reason to wait any longer than PS Service's own fast endpoints.
_DISCOVERY_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)

_UNEXPECTED_RESOURCE_METADATA_SHAPE_MSG = (
    "PS Service returned an unexpected resource-metadata shape"
)
_UNEXPECTED_DISCOVERY_DOCUMENT_SHAPE_MSG = (
    "the issuer returned an unexpected openid-configuration shape"
)

# The error must name both places a client id may be set. Kept as a module-level
# constant so tests can assert its exact text, as `http_client.py` does for its own
# unexpected-response message.
_NO_CLIENT_ID_MSG = (
    "No OIDC client id is configured: it is not present in the resource metadata, "
    "and no override is set for auth.client_id in targets.toml."
)

_NO_AUTHORIZATION_SERVERS_MSG = (
    "PS Service's resource metadata lists no authorization_servers, and no override "
    "is set for auth.issuer in targets.toml."
)

# The standard OIDC scope that signals "issue a refresh token too". Without it, most
# IdPs (Entra ID included) mint an access-token-only response, leaving
# `TokenBundle.refresh_token` permanently `None` so every later
# `ensure_valid_access_token()` call fails closed once the access token expires.
_OFFLINE_ACCESS_SCOPE = "offline_access"

# Module-local on purpose: duplicated from `http_client.py`'s private
# `_LOOPBACK_HOSTNAMES` rather than shared, because it serves a *warn* semantic there
# (`_should_warn_insecure`) and a *refuse* semantic here (`_assert_secure_or_loopback`).
# Three identical literal strings is under the codebase's third-occurrence DRY threshold.
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class ProtectedResourceMetadata:
    """RFC 9728 Protected Resource Metadata as served by PS Service.

    Mirrors PS Service's response model field-for-field without importing it, so
    `ps_cli` stays decoupled from `ps_service`. Immutable.

    Attributes:
        resource: The protected resource identifier PS Service reports.
        authorization_servers: Issuer URLs of the authorization servers whose tokens
            PS Service accepts; may be empty.
        scopes_supported: Scopes PS Service advertises; may be empty.
        ps_cli_client_id: OIDC client id for ps-cli, or `None` when PS Service's
            `PS_AUTH_CLI_CLIENT_ID` is unset (the server then omits the field).
    """

    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str]
    ps_cli_client_id: str | None


@dataclass(frozen=True)
class OidcDiscoveryDocument:
    """The three `/.well-known/openid-configuration` fields device-flow login needs.

    Every other field an IdP's discovery document may return is ignored. The document
    comes from a third-party IdP, so it is parsed defensively with `isinstance` checks
    rather than a bare `cast`. Immutable.

    Attributes:
        issuer: The issuer identifier the IdP reports.
        device_authorization_endpoint: Device-authorization URL, or `None` when the IdP
            does not support device flow.
        token_endpoint: Token URL, or `None` when the IdP does not advertise one.
    """

    issuer: str
    device_authorization_endpoint: str | None
    token_endpoint: str | None


@dataclass(frozen=True)
class ResolvedAuthParameters:
    """The fully resolved set of OIDC parameters device-flow login needs to run.

    `audience` comes only from the `auth.audience` override: RFC 8628 device
    authorization has no standard audience discovery, so there is no discovered
    default. Immutable.

    Attributes:
        issuer: The issuer URL used for discovery; https or loopback.
        client_id: The OIDC client id to authenticate as.
        scopes: Requested scopes; always includes `offline_access`, no duplicates.
        audience: The `auth.audience` override, or `None` when not configured.
        device_authorization_endpoint: Device-authorization URL; https or loopback.
        token_endpoint: Token URL; https or loopback.
    """

    issuer: str
    client_id: str
    scopes: tuple[str, ...]
    audience: str | None
    device_authorization_endpoint: str
    token_endpoint: str


def _raise_connection_error(url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `CannotVerifyError` for a connect failure to `url`.

    Reports a certificate-verification failure (with the trust hint) separately from
    a plain unreachable server. Kept here rather than reusing `PsServiceClient`
    because discovery targets PS Service's metadata endpoint and an arbitrary IdP,
    and no credential exists yet to build a client with.
    """
    detail = type(cause).__name__
    if is_certificate_verification_error(cause):
        raise CannotVerifyError(
            msg=f"Could not verify the TLS certificate of {url}.",
            hint=CERTIFICATE_ERROR_HINT,
            detail=detail,
        ) from cause
    raise CannotVerifyError(
        msg=f"Could not reach {url}.",
        hint="check the URL and that the server is running",
        detail=detail,
    ) from cause


def _raise_read_timeout_error(url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `CannotVerifyError` for a read timeout waiting on `url`."""
    raise CannotVerifyError(
        msg=f"{url} did not respond in time.", detail=type(cause).__name__
    ) from cause


def _assert_secure_or_loopback(url: str, *, what: str) -> None:
    """Raise `PsCliError` if `url` is neither `https://` nor a loopback address.

    Unlike `http_client.py::_should_warn_insecure`, which only warns about PS
    Service's own URL, this refuses: `resolve_auth_parameters` applies it to the
    resolved issuer and to the discovery document's endpoints, which are where a
    device code, refresh token or access token is actually sent. `what` names the
    parameter in the raised message (e.g. `"issuer"`).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in _LOOPBACK_HOSTNAMES:
        raise PsCliError(
            msg=f"{what} '{url}' is not https:// and not a loopback address",
            hint="refusing to send credentials over an insecure connection",
        )


def _get_json(url: str, *, transport: httpx.BaseTransport | None) -> object:
    """`GET url` over a short-lived `httpx.Client`, returning the parsed JSON body.

    Raises `CannotVerifyError` (a `PsCliError`) on a connect failure, a read timeout, or
    a 5xx status, and a plain `PsCliError` on any other non-2xx status or a non-JSON
    body. This is the shared network-error mapping for both discovery fetches.
    """
    with httpx.Client(
        timeout=_DISCOVERY_TIMEOUT, transport=transport, verify=build_ssl_context()
    ) as client:
        try:
            response = client.get(url)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(url, exc)
    if not response.is_success:
        msg = f"{url} returned an unexpected error response (status {response.status_code})."
        if response.is_server_error:
            raise CannotVerifyError(msg=msg, detail=f"status {response.status_code}")
        raise PsCliError(msg=msg)
    try:
        return response.json()
    except ValueError as exc:
        raise PsCliError(msg=f"{url} returned a response that is not valid JSON.") from exc


def _parse_protected_resource_metadata(payload: object) -> ProtectedResourceMetadata:
    """Parse a `GET /.well-known/oauth-protected-resource` 200 body.

    Raises `PsCliError` if the payload is not an object, or `resource`,
    `authorization_servers`, `scopes_supported` or `ps_cli_client_id` has the wrong type.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_RESOURCE_METADATA_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    resource = body.get("resource")
    authorization_servers_raw = body.get("authorization_servers")
    scopes_supported_raw = body.get("scopes_supported")
    ps_cli_client_id_raw = body.get("ps_cli_client_id")
    if (
        not isinstance(resource, str)
        or not isinstance(authorization_servers_raw, list)
        or not isinstance(scopes_supported_raw, list)
    ):
        raise PsCliError(msg=_UNEXPECTED_RESOURCE_METADATA_SHAPE_MSG)
    authorization_servers_items = cast("list[object]", authorization_servers_raw)
    scopes_supported_items = cast("list[object]", scopes_supported_raw)
    if not all(isinstance(item, str) for item in authorization_servers_items) or not all(
        isinstance(item, str) for item in scopes_supported_items
    ):
        raise PsCliError(msg=_UNEXPECTED_RESOURCE_METADATA_SHAPE_MSG)
    if ps_cli_client_id_raw is not None and not isinstance(ps_cli_client_id_raw, str):
        raise PsCliError(msg=_UNEXPECTED_RESOURCE_METADATA_SHAPE_MSG)
    return ProtectedResourceMetadata(
        resource=resource,
        authorization_servers=cast("list[str]", authorization_servers_items),
        scopes_supported=cast("list[str]", scopes_supported_items),
        ps_cli_client_id=ps_cli_client_id_raw,
    )


def fetch_protected_resource_metadata(
    service_url: str, *, transport: httpx.BaseTransport | None = None
) -> ProtectedResourceMetadata:
    """Fetch PS Service's OAuth protected-resource metadata.

    Args:
        service_url: PS Service base URL without a trailing slash; the request goes to
            `<service_url>/.well-known/oauth-protected-resource`.
        transport: Optional `httpx` transport that replaces the network (tests only).

    Returns:
        The parsed `ProtectedResourceMetadata`.

    Raises:
        CannotVerifyError: PS Service could not be reached, failed the TLS check,
            timed out, or answered 5xx.
        PsCliError: PS Service answered another non-2xx status, a non-JSON body, or a
            body of the wrong shape.

    Side Effects:
        One HTTPS GET.
    """
    url = f"{service_url}{_RESOURCE_METADATA_PATH}"
    payload = _get_json(url, transport=transport)
    return _parse_protected_resource_metadata(payload)


def resolve_client_id(metadata: ProtectedResourceMetadata, override: AuthOverrides | None) -> str:
    """Resolve the OIDC client id: the override wins, else the one PS Service advertises.

    Args:
        metadata: PS Service's resource metadata; supplies `ps_cli_client_id`.
        override: The context's `auth` overrides, or `None` when none are configured.

    Returns:
        `override.client_id` if set, else `metadata.ps_cli_client_id`.

    Raises:
        PsCliError: Neither is set; the message names both places a client id may be
            configured.
    """
    if override is not None and override.client_id is not None:
        return override.client_id
    if metadata.ps_cli_client_id is not None:
        return metadata.ps_cli_client_id
    raise PsCliError(msg=_NO_CLIENT_ID_MSG)


def _parse_openid_discovery_document(payload: object) -> OidcDiscoveryDocument:
    """Parse a `GET /.well-known/openid-configuration` 200 body.

    Raises `PsCliError` if the payload is not an object, `issuer` is missing or not a
    string, or either endpoint is present but not a string.

    Only `issuer`/`device_authorization_endpoint`/`token_endpoint` are read; every
    other field the IdP returns is ignored. `issuer` is required; the other two are
    optional (an IdP that does not support device flow may omit
    `device_authorization_endpoint` entirely -- that is a valid, parseable document,
    just one `resolve_auth_parameters()` later rejects because login needs it).
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_DISCOVERY_DOCUMENT_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    issuer = body.get("issuer")
    if not isinstance(issuer, str):
        raise PsCliError(msg=_UNEXPECTED_DISCOVERY_DOCUMENT_SHAPE_MSG)
    device_authorization_endpoint_raw = body.get("device_authorization_endpoint")
    token_endpoint_raw = body.get("token_endpoint")
    if device_authorization_endpoint_raw is not None and not isinstance(
        device_authorization_endpoint_raw, str
    ):
        raise PsCliError(msg=_UNEXPECTED_DISCOVERY_DOCUMENT_SHAPE_MSG)
    if token_endpoint_raw is not None and not isinstance(token_endpoint_raw, str):
        raise PsCliError(msg=_UNEXPECTED_DISCOVERY_DOCUMENT_SHAPE_MSG)
    return OidcDiscoveryDocument(
        issuer=issuer,
        device_authorization_endpoint=device_authorization_endpoint_raw,
        token_endpoint=token_endpoint_raw,
    )


def _normalize_issuer_for_discovery_url(issuer: str) -> str:
    """Strip exactly one trailing slash from `issuer`, if present.

    Some IdPs' fixed per-application issuer shape ends in `/` (Authentik's own
    `.../application/o/<slug>/`, for one). `fetch_openid_configuration` builds its
    request URL by string concatenation, so an un-normalized issuer produces a
    double slash before `.well-known` (`.../<slug>//.well-known/openid-configuration`),
    which the discovery endpoint 404s on. Only one trailing slash is stripped --
    not `.rstrip("/")` -- since an issuer is a URL, not a path to be collapsed;
    a hypothetical `.../slug//` is left as the caller supplied it beyond the first
    strip, same as this module treats every other issuer string as opaque.
    """
    if issuer.endswith("/"):
        return issuer[:-1]
    return issuer


def fetch_openid_configuration(
    issuer: str, *, transport: httpx.BaseTransport | None = None
) -> OidcDiscoveryDocument:
    """Fetch an IdP's OpenID Connect discovery document.

    Args:
        issuer: The IdP issuer URL; one trailing slash is ignored when building the
            request URL `<issuer>/.well-known/openid-configuration`.
        transport: Optional `httpx` transport that replaces the network (tests only).

    Returns:
        The parsed `OidcDiscoveryDocument`.

    Raises:
        CannotVerifyError: The IdP could not be reached, failed the TLS check, timed
            out, or answered 5xx.
        PsCliError: The IdP answered another non-2xx status, a non-JSON body, or a
            body of the wrong shape.

    Side Effects:
        One HTTPS GET.
    """
    url = f"{_normalize_issuer_for_discovery_url(issuer)}{_OPENID_CONFIGURATION_PATH}"
    payload = _get_json(url, transport=transport)
    return _parse_openid_discovery_document(payload)


def resolve_auth_parameters(
    service_url: str,
    override: AuthOverrides | None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ResolvedAuthParameters:
    """Resolve every OIDC parameter device-flow login needs; overrides win over discovery.

    Fetches PS Service's resource metadata, then resolves the client id
    (`resolve_client_id`), the issuer (`override.issuer`, else the first of
    `metadata.authorization_servers`) and the scopes (`override.scopes`, else
    `metadata.scopes_supported`, with `offline_access` appended when absent so the
    login yields a refresh token). It then fetches `openid-configuration` from the
    resolved issuer (never the discovered one when an override replaced it) and reads
    its device-authorization and token endpoints. The issuer and both endpoints must
    be `https://` or loopback; the issuer is checked before it is contacted.

    Args:
        service_url: PS Service base URL without a trailing slash.
        override: The context's `auth` overrides, or `None` when none are configured.
        transport: Optional `httpx` transport that replaces the network (tests only).

    Returns:
        The `ResolvedAuthParameters` ready for device-flow login.

    Raises:
        CannotVerifyError: PS Service or the IdP could not be reached, failed the TLS
            check, timed out, or answered 5xx.
        PsCliError: No client id is configured; no issuer is discoverable and none is
            overridden; the issuer is not https/loopback; the IdP lacks a device
            authorization endpoint or a token endpoint; an endpoint is not
            https/loopback; or a discovery response is malformed.

    Side Effects:
        Two HTTPS GETs (resource metadata, then openid-configuration).
    """
    metadata = fetch_protected_resource_metadata(service_url, transport=transport)
    client_id = resolve_client_id(metadata, override)

    if override is not None and override.issuer is not None:
        issuer = override.issuer
    elif metadata.authorization_servers:
        issuer = metadata.authorization_servers[0]
    else:
        raise PsCliError(msg=_NO_AUTHORIZATION_SERVERS_MSG)
    # Refuse before contacting a non-loopback http issuer at all, not just before a
    # token is sent.
    _assert_secure_or_loopback(issuer, what="issuer")

    scopes = (
        override.scopes
        if override is not None and override.scopes is not None
        else tuple(metadata.scopes_supported)
    )
    # Always ask for `offline_access` so a device-flow login can be refreshed, on top
    # of the scopes PS Service or an `auth.scopes` override already ask for. Appended
    # here rather than set in deploy config so no deployment can lose it; not added
    # twice when the server or override already lists it.
    if _OFFLINE_ACCESS_SCOPE not in scopes:
        scopes = (*scopes, _OFFLINE_ACCESS_SCOPE)
    audience = override.audience if override is not None else None

    discovery_document = fetch_openid_configuration(issuer, transport=transport)
    if discovery_document.device_authorization_endpoint is None:
        raise PsCliError(
            msg=f"issuer '{issuer}' does not support device authorization "
            "(its openid-configuration has no device_authorization_endpoint)."
        )
    # Checked right after parsing the discovery document, before any later code POSTs
    # to either endpoint.
    _assert_secure_or_loopback(
        discovery_document.device_authorization_endpoint, what="device_authorization_endpoint"
    )
    if discovery_document.token_endpoint is None:
        raise PsCliError(
            msg=f"issuer '{issuer}' does not advertise a token_endpoint in its "
            "openid-configuration."
        )
    _assert_secure_or_loopback(discovery_document.token_endpoint, what="token_endpoint")

    return ResolvedAuthParameters(
        issuer=issuer,
        client_id=client_id,
        scopes=scopes,
        audience=audience,
        device_authorization_endpoint=discovery_document.device_authorization_endpoint,
        token_endpoint=discovery_document.token_endpoint,
    )
