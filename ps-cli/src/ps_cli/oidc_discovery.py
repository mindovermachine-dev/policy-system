"""ps-cli OIDC discovery: PS-Service resource metadata + IdP openid-configuration.

Issue #57 Slices 5-6. Two distinct discovery documents are fetched here, from two
distinct servers, and must not be confused:

- `ProtectedResourceMetadata` (RFC 9728) is served by **PS Service itself**, at
  `<service_url>/.well-known/oauth-protected-resource`
  (`ps_service.api.protected_resource`) -- it names the authorization server(s) that
  can mint tokens PS Service accepts, plus (optionally) `ps_cli_client_id`, this
  project's own extension field. Vendored field-for-field from
  `ps_service.api.models.ProtectedResourceMetadata` (`ps-service/src/ps_service/api/
  models.py:350-368`) rather than imported -- `ps_cli` must never import
  `ps_service.*` (L2 Project Structure's decoupling rule; enforced by this repo's own
  `test_architecture_boundary.py`).
- `OidcDiscoveryDocument` (RFC 8414 / OIDC Discovery) is served by the **IdP**
  (authorization server) named in that resource metadata, at
  `<issuer>/.well-known/openid-configuration` -- it names the endpoints device-flow
  login actually calls (`device_authorization_endpoint`, `token_endpoint`).

`resolve_auth_parameters()` ties the two together plus `AuthOverrides`
(`ps_cli.targets`, issue #57 Slice 1) into one `ResolvedAuthParameters`: an
override always wins over a discovered default, and every "nothing was found and
there is no override either" case fails closed with an actionable `PsCliError`
rather than an `IndexError`/`KeyError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast
from urllib.parse import urlparse

import httpx

from ps_cli.errors import PsCliError

if TYPE_CHECKING:
    from ps_cli.targets import AuthOverrides

_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
_OPENID_CONFIGURATION_PATH = "/.well-known/openid-configuration"

# Matches `http_client.py`'s own fast-path timeout convention (`_STATUS_POLL_TIMEOUT`)
# -- both discovery documents are small, static JSON bodies with no reason to wait
# any longer than PS Service's own fast endpoints.
_DISCOVERY_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)

_UNEXPECTED_RESOURCE_METADATA_SHAPE_MSG = (
    "PS Service returned an unexpected resource-metadata shape"
)
_UNEXPECTED_DISCOVERY_DOCUMENT_SHAPE_MSG = (
    "the issuer returned an unexpected openid-configuration shape"
)

# AC-BI-004's literal wording: the error must name both places a client id may be
# set. Kept as a module-level constant so tests can assert its exact text, mirroring
# `http_client.py::_UNEXPECTED_RESPONSE_SHAPE_MSG`'s own precedent.
_NO_CLIENT_ID_MSG = (
    "No OIDC client id is configured: it is not present in the resource metadata, "
    "and no override is set for auth.client_id in targets.toml."
)

_NO_AUTHORIZATION_SERVERS_MSG = (
    "PS Service's resource metadata lists no authorization_servers, and no override "
    "is set for auth.issuer in targets.toml."
)

# Issue #119: the standard OIDC scope that signals "issue a refresh token too" --
# without it, most IdPs (confirmed against Entra ID) mint an access-token-only
# response, leaving `TokenBundle.refresh_token` permanently `None` and every later
# `ensure_valid_access_token()` call fail-closed the moment that access token expires.
_OFFLINE_ACCESS_SCOPE = "offline_access"

# Issue #57 Slice 21 (AC-BI-017): module-local, deliberately duplicated from
# `http_client.py`'s own `_LOOPBACK_HOSTNAMES` rather than imported/shared -- that
# constant is private to `http_client.py` and serves a *warn* semantic there
# (`_should_warn_insecure`) vs. a *refuse* semantic here (`_assert_secure_or_loopback`).
# Three identical literal strings is well under this codebase's own third-occurrence
# DRY threshold.
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class ProtectedResourceMetadata:
    """RFC 9728 Protected Resource Metadata.

    Vendored mirror of `ps_service.api.models.ProtectedResourceMetadata`,
    field-for-field (never imported; see this module's own docstring).
    `ps_cli_client_id` is `None` when PS Service's `PS_AUTH_CLI_CLIENT_ID` is unset
    (the server omits the field entirely via `response_model_exclude_none=True` in
    that case).
    """

    resource: str
    authorization_servers: list[str]
    scopes_supported: list[str]
    ps_cli_client_id: str | None


@dataclass(frozen=True)
class OidcDiscoveryDocument:
    """The three `/.well-known/openid-configuration` fields this issue needs.

    Every other field an IdP's discovery document may return is ignored --
    `tomllib`-style "trusted shape" parsing (`targets.py:70-77`'s own comment)
    applies equally to this external, less-trusted document, except here the parse
    is defensive (`isinstance` checks), not a bare `cast`, since this document comes
    from a third-party IdP, not this codebase's own writer.
    """

    issuer: str
    device_authorization_endpoint: str | None
    token_endpoint: str | None


@dataclass(frozen=True)
class ResolvedAuthParameters:
    """The fully-resolved set of OIDC parameters device-flow login needs to run.

    `audience` is simply `override.audience` (AC-BI-003) -- RFC 8628 device
    authorization has no standard audience-discovery mechanism, so there is no
    discovered default for it.
    """

    issuer: str
    client_id: str
    scopes: tuple[str, ...]
    audience: str | None
    device_authorization_endpoint: str
    token_endpoint: str


def _raise_connection_error(url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `PsCliError` for a connect failure to `url`.

    Mirrors `http_client.py::_raise_connection_error`'s wording style
    (`http_client.py:119-124`), duplicated here rather than imported -- this module
    talks to two different logical resources (PS Service's own metadata endpoint,
    and an arbitrary IdP's discovery endpoint), neither of which is `PsServiceClient`
    itself, so constructing one just to make a single GET before any credential
    exists would be the wrong layering.
    """
    raise PsCliError(
        msg=f"Could not reach {url}.",
        hint="check the URL and that the server is running",
    ) from cause


def _raise_read_timeout_error(url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `PsCliError` for a read-timeout waiting on `url`.

    Mirrors `http_client.py::_raise_read_timeout_error`'s wording style
    (`http_client.py:127-129`).
    """
    raise PsCliError(msg=f"{url} did not respond in time.") from cause


def _assert_secure_or_loopback(url: str, *, what: str) -> None:
    """Raise `PsCliError` if `url` is neither `https://` nor a loopback address (AC-BI-017).

    Unlike `http_client.py::_should_warn_insecure` (which only warns about
    `service_url`, PS Service's own URL), this *refuses* -- `resolve_auth_parameters`
    calls this on the resolved issuer and on the discovery document's
    `device_authorization_endpoint`/`token_endpoint`, since those are the endpoints a
    device-code, refresh token, or access token is actually sent to. `what` names the
    parameter in the raised message (e.g. `"issuer"`, `"device_authorization_endpoint"`).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in _LOOPBACK_HOSTNAMES:
        raise PsCliError(
            msg=f"{what} '{url}' is not https:// and not a loopback address",
            hint="refusing to send credentials over an insecure connection",
        )


def _get_json(url: str, *, transport: httpx.BaseTransport | None) -> object:
    """`GET url` over a short-lived `httpx.Client`, returning the parsed JSON body.

    Raises `PsCliError` on a connect failure, a read timeout, a non-2xx status, or a
    non-JSON body -- the shared network-error mapping both `fetch_protected_
    resource_metadata()` and `fetch_openid_configuration()` need.
    """
    with httpx.Client(timeout=_DISCOVERY_TIMEOUT, transport=transport) as client:
        try:
            response = client.get(url)
        except httpx.ReadTimeout as exc:
            _raise_read_timeout_error(url, exc)
        except httpx.TransportError as exc:
            _raise_connection_error(url, exc)
    if not response.is_success:
        raise PsCliError(
            msg=f"{url} returned an unexpected error response (status {response.status_code})."
        )
    try:
        return response.json()
    except ValueError as exc:
        raise PsCliError(msg=f"{url} returned a response that is not valid JSON.") from exc


def _parse_protected_resource_metadata(payload: object) -> ProtectedResourceMetadata:
    """Parse a `GET /.well-known/oauth-protected-resource` 200 body.

    Raises `PsCliError` (generic, defensive) if the shape does not match.
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
    """`GET <service_url>/.well-known/oauth-protected-resource` -- PS Service's own endpoint.

    Raises `PsCliError` on connection failure/timeout, a non-2xx response, or a
    malformed body.
    """
    url = f"{service_url}{_RESOURCE_METADATA_PATH}"
    payload = _get_json(url, transport=transport)
    return _parse_protected_resource_metadata(payload)


def resolve_client_id(metadata: ProtectedResourceMetadata, override: AuthOverrides | None) -> str:
    """Resolve the OIDC client id: `override.client_id` wins, else `ps_cli_client_id`.

    `override.client_id` wins if set, else `metadata.ps_cli_client_id` is used.
    Raises `PsCliError` naming both places a client id may be set (AC-BI-004) if
    neither is present.
    """
    if override is not None and override.client_id is not None:
        return override.client_id
    if metadata.ps_cli_client_id is not None:
        return metadata.ps_cli_client_id
    raise PsCliError(msg=_NO_CLIENT_ID_MSG)


def _parse_openid_discovery_document(payload: object) -> OidcDiscoveryDocument:
    """Parse a `GET /.well-known/openid-configuration` 200 body.

    Only `issuer`/`device_authorization_endpoint`/`token_endpoint` are read; every
    other field the IdP returns is ignored. `issuer` is required; the other two are
    optional (an IdP that does not support device flow may omit
    `device_authorization_endpoint` entirely -- that is a valid, parseable document,
    just one `resolve_auth_parameters()` later rejects for this issue's purposes).
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


def fetch_openid_configuration(
    issuer: str, *, transport: httpx.BaseTransport | None = None
) -> OidcDiscoveryDocument:
    """`GET <issuer>/.well-known/openid-configuration` -- the IdP's own endpoint.

    Raises `PsCliError` on connection failure/timeout, a non-2xx response, or a
    malformed body.
    """
    url = f"{issuer}{_OPENID_CONFIGURATION_PATH}"
    payload = _get_json(url, transport=transport)
    return _parse_openid_discovery_document(payload)


def resolve_auth_parameters(
    service_url: str,
    override: AuthOverrides | None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ResolvedAuthParameters:
    """Resolve every OIDC parameter device-flow login needs, override-wins-else-discovered.

    Fetches PS Service's resource metadata, resolves `client_id` (`resolve_client_id`),
    resolves `issuer` (`override.issuer` if set, else `metadata.authorization_servers[0]`
    -- raising `PsCliError` if `authorization_servers` is empty and there is no
    override, failing closed rather than raising `IndexError`), resolves `scopes`
    (`override.scopes` if set, else `tuple(metadata.scopes_supported)`, always with
    `offline_access` appended if not already present -- issue #119, AC-BI-001/002),
    then fetches `openid-configuration` from the *resolved* issuer (never the discovered one when
    an override replaced it) and reads `device_authorization_endpoint`/
    `token_endpoint`, raising `PsCliError` naming the issuer if either is absent
    (AC-BI-005). Also refuses (`_assert_secure_or_loopback`, AC-BI-017) a resolved
    issuer, `device_authorization_endpoint`, or `token_endpoint` that is neither
    `https://` nor a loopback address -- checked on the issuer before ever fetching
    its openid-configuration, and on each endpoint immediately after parsing that
    document, before any later code ever POSTs to either.
    """
    metadata = fetch_protected_resource_metadata(service_url, transport=transport)
    client_id = resolve_client_id(metadata, override)

    if override is not None and override.issuer is not None:
        issuer = override.issuer
    elif metadata.authorization_servers:
        issuer = metadata.authorization_servers[0]
    else:
        raise PsCliError(msg=_NO_AUTHORIZATION_SERVERS_MSG)
    # AC-BI-017: refuse before ever contacting a non-loopback-http issuer at all --
    # before the openid-configuration fetch below, not just before a token is sent.
    _assert_secure_or_loopback(issuer, what="issuer")

    scopes = (
        override.scopes
        if override is not None and override.scopes is not None
        else tuple(metadata.scopes_supported)
    )
    # Issue #119, AC-BI-001/002: always ask for `offline_access` so a device-flow
    # login can be refreshed, on top of whatever PS Service's own resource-metadata
    # scopes (or an operator's `auth.scopes` override) already ask for -- appended
    # here, not folded into `PS_AUTH_SCOPES`/deploy config, so it can never regress
    # per-deployment (see #119's Solution). Deduplicated: a server or override that
    # already lists it must not send it twice.
    if _OFFLINE_ACCESS_SCOPE not in scopes:
        scopes = (*scopes, _OFFLINE_ACCESS_SCOPE)
    audience = override.audience if override is not None else None

    discovery_document = fetch_openid_configuration(issuer, transport=transport)
    if discovery_document.device_authorization_endpoint is None:
        raise PsCliError(
            msg=f"issuer '{issuer}' does not support device authorization "
            "(its openid-configuration has no device_authorization_endpoint)."
        )
    # AC-BI-017: checked immediately after parsing the discovery document, before
    # Slice 9/10/16 code ever POSTs to either endpoint.
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
