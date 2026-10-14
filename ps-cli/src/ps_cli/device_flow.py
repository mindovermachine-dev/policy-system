"""ps-cli RFC 8628 device-authorization flow: request, poll, and token conversion.

The module is I/O-free apart from its HTTP calls: it never prints. The `auth login`
handler does all user-facing output, mirroring the split between `PsServiceClient`
(fetch and parse) and the command handlers (print).

- `request_device_authorization()` -- `POST <device_authorization_endpoint>`; mints a
  `DeviceAuthorization` (`device_code`, `user_code`, `verification_uri`, ...).
- `poll_for_token()` -- a hand-rolled polling loop against `<token_endpoint>` that
  honours `authorization_pending` and `slow_down` (RFC 8628 section 3.5) and raises an
  actionable `PsCliError` on `expired_token`, `access_denied` or any unrecognized code.
- `complete_device_login()` -- runs the two in order. It takes an
  `on_device_authorization` callback that is invoked with the freshly minted
  `DeviceAuthorization` before the blocking poll starts, so the caller can show the
  user code and verification URI at the right moment; it returns the `TokenResponse`
  the poll produced.
- `ensure_valid_access_token()` -- turns the stored refresh token into a usable access
  token for authenticated commands, caching it in memory for the invocation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast

import httpx

from ps_cli import oidc_discovery
from ps_cli.credentials import TokenBundle
from ps_cli.errors import CannotVerifyError, PsCliError
from ps_cli.tls import (
    CERTIFICATE_ERROR_HINT,
    build_ssl_context,
    is_certificate_verification_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_cli.credentials import CredentialStore
    from ps_cli.oidc_discovery import ResolvedAuthParameters
    from ps_cli.targets import AuthOverrides

# Same short, uniform bound as `oidc_discovery.py`'s discovery timeout: the
# device-authorization request and each token-endpoint poll are small, fast JSON
# round-trips with no reason to wait longer than PS Service's own fast endpoints.
_DEVICE_FLOW_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)

_GRANT_TYPE_DEVICE_CODE = "urn:ietf:params:oauth:grant-type:device_code"

# RFC 8628 section 3.5's recommended backoff increment on `slow_down`.
_SLOW_DOWN_INCREMENT_SECONDS = 5

_UNEXPECTED_DEVICE_AUTHORIZATION_SHAPE_MSG = (
    "the issuer returned an unexpected device-authorization response shape"
)
_UNEXPECTED_GRANT_RESPONSE_SHAPE_MSG = "the issuer returned an unexpected grant-response shape"

# Clock-skew and in-flight-request buffer for the in-memory `AccessTokenCache`: a
# request in flight must not race a token that expires mid-call.
_EXPIRY_LEEWAY_SECONDS = 30

_RELOGIN_HINT = "run `ps-cli auth login`"
_REFRESH_FAILED_MSG = "stored credentials could not be refreshed"


@dataclass(frozen=True)
class DeviceAuthorization:
    """RFC 8628 section 3.2 device-authorization response: what the user must act on.

    Immutable.

    Attributes:
        device_code: Opaque code the CLI polls the token endpoint with; never shown.
        user_code: Short code the user types at the verification page.
        verification_uri: Page where the user enters `user_code`.
        verification_uri_complete: Verification URL with the code embedded, or `None`
            when the IdP does not supply one.
        expires_in: Seconds until `device_code` expires.
        interval: Minimum seconds between token polls.
    """

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


@dataclass(frozen=True)
class TokenResponse:
    """The subset of the RFC 8628 section 3.5 token-endpoint success response ps-cli uses.

    Immutable.

    Attributes:
        access_token: The bearer access token.
        refresh_token: The refresh token, or `None` when the IdP issued none.
        expires_in: Access-token lifetime in seconds.
    """

    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclass
class AccessTokenCache:
    """One CLI invocation's in-memory access token; never persisted.

    A `PsServiceClient` creates one and passes it to `ensure_valid_access_token` on
    every authenticated request, so the token is reused within the invocation and
    discarded when the process exits. Because `expires_at` is checked on each use, a run
    that outlives the token's lifetime (a long ingest) refreshes again instead of
    sending an expired token. A fresh cache is empty, so the first request refreshes
    exactly once. Mutable.

    Attributes:
        token: The cached access token, or `None` before the first refresh.
        expires_at: Absolute Unix-epoch expiry of `token`, set with it on every
            refresh; `None` before the first refresh.
    """

    token: str | None = None
    expires_at: int | None = None


def _raise_connection_error(url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `PsCliError` for a connect failure to `url`.

    Kept separate from the discovery module's equivalent because this one targets the
    IdP's device-authorization and token endpoints and raises a plain `PsCliError`;
    a certificate-verification failure gets the trust hint.
    """
    if is_certificate_verification_error(cause):
        raise PsCliError(
            msg=f"Could not verify the TLS certificate of {url}.",
            hint=CERTIFICATE_ERROR_HINT,
        ) from cause
    raise PsCliError(
        msg=f"Could not reach {url}.",
        hint="check the URL and that the server is running",
    ) from cause


def _raise_read_timeout_error(url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `PsCliError` for a read-timeout waiting on `url`."""
    raise PsCliError(msg=f"{url} did not respond in time.") from cause


def _post_form(client: httpx.Client, url: str, data: dict[str, str]) -> httpx.Response:
    """`POST url` form-encoded via `client`, mapping connect/timeout failures to `PsCliError`."""
    try:
        return client.post(url, data=data)
    except httpx.ReadTimeout as exc:
        _raise_read_timeout_error(url, exc)
    except httpx.TransportError as exc:
        _raise_connection_error(url, exc)


def _parse_device_authorization(payload: object) -> DeviceAuthorization:
    """Parse a `POST <device_authorization_endpoint>` 200 body.

    The body comes from a third-party IdP, so it is validated with `isinstance` checks
    rather than trusted. Raises `PsCliError` if it is not an object, a required field
    (`device_code`, `user_code`, `verification_uri`, `expires_in`, `interval`) is
    missing or mistyped, or `verification_uri_complete` is present but not a string.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_DEVICE_AUTHORIZATION_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification_uri = body.get("verification_uri")
    verification_uri_complete_raw = body.get("verification_uri_complete")
    expires_in = body.get("expires_in")
    interval = body.get("interval")
    if (
        not isinstance(device_code, str)
        or not isinstance(user_code, str)
        or not isinstance(verification_uri, str)
        or not isinstance(expires_in, int)
        or not isinstance(interval, int)
    ):
        raise PsCliError(msg=_UNEXPECTED_DEVICE_AUTHORIZATION_SHAPE_MSG)
    if verification_uri_complete_raw is not None and not isinstance(
        verification_uri_complete_raw, str
    ):
        raise PsCliError(msg=_UNEXPECTED_DEVICE_AUTHORIZATION_SHAPE_MSG)
    return DeviceAuthorization(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=verification_uri_complete_raw,
        expires_in=expires_in,
        interval=interval,
    )


def request_device_authorization(
    params: ResolvedAuthParameters, *, transport: httpx.BaseTransport | None = None
) -> DeviceAuthorization:
    """Start a device-authorization login at the IdP.

    Sends `client_id` and the space-joined `scope` always; sends `audience` only when
    `params.audience` is set, never as an empty string. Prints nothing; the caller
    shows the user code and verification URI.

    Args:
        params: Resolved OIDC parameters; `device_authorization_endpoint` is the target.
        transport: Optional `httpx` transport that replaces the network (tests only).

    Returns:
        The `DeviceAuthorization` the IdP issued.

    Raises:
        PsCliError: The endpoint is unreachable, fails the TLS check or times out; it
            answers a non-2xx status or a non-JSON body; or the body has an unexpected
            shape.

    Side Effects:
        One HTTPS POST to `params.device_authorization_endpoint`.
    """
    data: dict[str, str] = {
        "client_id": params.client_id,
        "scope": " ".join(params.scopes),
    }
    if params.audience is not None:
        data["audience"] = params.audience
    with httpx.Client(
        timeout=_DEVICE_FLOW_TIMEOUT, transport=transport, verify=build_ssl_context()
    ) as client:
        response = _post_form(client, params.device_authorization_endpoint, data)
    if not response.is_success:
        raise PsCliError(
            msg=f"{params.device_authorization_endpoint} returned an unexpected error "
            f"response (status {response.status_code})."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PsCliError(
            msg=f"{params.device_authorization_endpoint} returned a response that is "
            "not valid JSON."
        ) from exc
    return _parse_device_authorization(payload)


def _parse_token_response(payload: object) -> TokenResponse:
    """Parse a `POST <token_endpoint>` 200 body into a `TokenResponse`.

    Raises `PsCliError` if the body is not an object, `access_token` is not a string,
    `expires_in` is not an integer, or `refresh_token` is present but not a string.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_GRANT_RESPONSE_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    access_token = body.get("access_token")
    refresh_token_raw = body.get("refresh_token")
    expires_in = body.get("expires_in")
    if not isinstance(access_token, str) or not isinstance(expires_in, int):
        raise PsCliError(msg=_UNEXPECTED_GRANT_RESPONSE_SHAPE_MSG)
    if refresh_token_raw is not None and not isinstance(refresh_token_raw, str):
        raise PsCliError(msg=_UNEXPECTED_GRANT_RESPONSE_SHAPE_MSG)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token_raw,
        expires_in=expires_in,
    )


def _extract_error_code(response: httpx.Response) -> str:
    """Read `{"error": "..."}` off a non-2xx token-endpoint response.

    Falls back to a synthetic `http-<status>` code (never raises) when the body is not
    JSON or carries no string `error` field, because a server may not return the
    documented shape. `_raise_for_error_code` treats any unrecognized code generically,
    so the fallback still yields an actionable error.
    """
    try:
        payload = response.json()
    except ValueError:
        return f"http-{response.status_code}"
    if not isinstance(payload, dict):
        return f"http-{response.status_code}"
    body = cast("dict[str, object]", payload)
    error_code = body.get("error")
    if isinstance(error_code, str):
        return error_code
    return f"http-{response.status_code}"


def _raise_for_error_code(error_code: str) -> NoReturn:
    """Raise the `PsCliError` that tells the user why the login failed.

    `expired_token` and `access_denied` get their own wording; any other code
    (including the `http-<status>` fallback) gets a generic error naming the raw code.
    Each carries a hint to run `ps-cli auth login` again.
    """
    if error_code == "expired_token":
        raise PsCliError(
            msg="the device code expired before login completed",
            hint="run `ps-cli auth login` again",
        )
    if error_code == "access_denied":
        raise PsCliError(
            msg="login was denied",
            hint="run `ps-cli auth login` again to retry",
        )
    raise PsCliError(
        msg=f"the issuer rejected the device-flow login (error: {error_code}).",
        hint="run `ps-cli auth login` again",
    )


def poll_for_token(
    params: ResolvedAuthParameters,
    device_auth: DeviceAuthorization,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> TokenResponse:
    """Poll the token endpoint until the user finishes the device-flow login or it fails.

    The wait starts at `device_auth.interval` seconds and grows by five seconds on
    every `slow_down` response (RFC 8628 section 3.5); `authorization_pending` keeps
    polling at the current interval.

    Args:
        params: Resolved OIDC parameters; `token_endpoint` and `client_id` are used.
        device_auth: The authorization returned by `request_device_authorization`.
        transport: Optional `httpx` transport that replaces the network (tests only).
        sleep: Called with the number of seconds to wait between polls; injectable so
            tests never wait on a real clock.

    Returns:
        The `TokenResponse` from the first successful poll.

    Raises:
        PsCliError: The endpoint is unreachable, fails the TLS check or times out; a
            success body is not valid JSON or has an unexpected shape; or the IdP
            answers `expired_token`, `access_denied` or any other unrecognized error.

    Side Effects:
        Repeated HTTPS POSTs to `params.token_endpoint`; blocks in `sleep` between
        polls, potentially until the device code expires.
    """
    interval = device_auth.interval
    data = {
        "grant_type": _GRANT_TYPE_DEVICE_CODE,
        "device_code": device_auth.device_code,
        "client_id": params.client_id,
    }
    with httpx.Client(
        timeout=_DEVICE_FLOW_TIMEOUT, transport=transport, verify=build_ssl_context()
    ) as client:
        while True:
            response = _post_form(client, params.token_endpoint, data)
            if response.is_success:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise PsCliError(
                        msg=f"{params.token_endpoint} returned a response that is not valid JSON."
                    ) from exc
                return _parse_token_response(payload)
            error_code = _extract_error_code(response)
            if error_code == "authorization_pending":
                sleep(interval)
                continue
            if error_code == "slow_down":
                interval += _SLOW_DOWN_INCREMENT_SECONDS
                sleep(interval)
                continue
            _raise_for_error_code(error_code)


def complete_device_login(
    params: ResolvedAuthParameters,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_device_authorization: Callable[[DeviceAuthorization], None] | None = None,
) -> TokenResponse:
    """Run the full device-flow login: request authorization, then poll to completion.

    Args:
        params: Resolved OIDC parameters for the login.
        transport: Optional `httpx` transport that replaces the network (tests only).
        sleep: Called with the seconds to wait between polls; injectable for tests.
        on_device_authorization: Optional callback invoked with the new
            `DeviceAuthorization` before the blocking poll begins, so the caller can
            show the user code and verification URI at the right moment.

    Returns:
        The `TokenResponse` produced by the poll.

    Raises:
        PsCliError: Any failure of `request_device_authorization` or
            `poll_for_token`, including an expired or denied login.

    Side Effects:
        HTTPS requests to the IdP, a blocking poll, and whatever the callback does.
    """
    device_auth = request_device_authorization(params, transport=transport)
    if on_device_authorization is not None:
        on_device_authorization(device_auth)
    return poll_for_token(params, device_auth, transport=transport, sleep=sleep)


def token_bundle_from_response(response: TokenResponse, issuer: str) -> TokenBundle:
    """Convert a login `TokenResponse` into the `TokenBundle` the credential store keeps.

    Only the refresh token and issuer persist; the access token and its lifetime are
    never written to the store (the access token lives in memory in `AccessTokenCache`).
    Storing the result is the caller's job.

    Args:
        response: The token response from a completed login.
        issuer: The issuer URL the tokens came from.

    Returns:
        A `TokenBundle` holding `response.refresh_token` (possibly `None`) and `issuer`.

    Raises:
        Nothing.

    Side Effects:
        None.
    """
    return TokenBundle(refresh_token=response.refresh_token, issuer=issuer)


def _raise_no_stored_credentials(context: str) -> NoReturn:
    """Raise the fail-closed `PsCliError` for a `context` with no stored credentials."""
    raise PsCliError(
        msg=f"no stored credentials for context '{context}'",
        hint=_RELOGIN_HINT,
    )


def _raise_refresh_failed() -> NoReturn:
    """Raise the fail-closed `PsCliError` for a refresh that could not complete.

    Shared by every "must log in again" outcome: no refresh token at all, a rejected
    refresh (non-2xx), or a malformed response body. All use the same wording.
    """
    raise PsCliError(msg=_REFRESH_FAILED_MSG, hint=_RELOGIN_HINT)


def _raise_refresh_unverifiable(detail: str) -> NoReturn:
    """Raise `CannotVerifyError` for a refresh that failed for reasons unrelated to the credential.

    Same wording as `_raise_refresh_failed`; only the exception type differs, which
    tells `auth status` the credential was never judged. `detail` is a transport
    exception class name or an HTTP status.
    """
    raise CannotVerifyError(msg=_REFRESH_FAILED_MSG, hint=_RELOGIN_HINT, detail=detail)


def _is_cache_stale(cache: AccessTokenCache) -> bool:
    """Return whether `cache`'s token is expired or expiring within `_EXPIRY_LEEWAY_SECONDS`.

    An empty cache (`expires_at is None`) counts as stale, so a fresh cache always
    falls through to a real refresh.
    """
    return cache.expires_at is None or cache.expires_at <= int(time.time()) + _EXPIRY_LEEWAY_SECONDS


def _refresh_tokens(
    params: ResolvedAuthParameters,
    refresh_token: str,
    *,
    transport: httpx.BaseTransport | None,
) -> TokenResponse:
    """Exchange `refresh_token` for a new access token at the IdP's token endpoint.

    Sends `params.scopes` (which already include `offline_access`) on every refresh so
    the IdP keeps issuing refresh tokens. The returned `refresh_token` is the one to
    persist: the rotated one if the IdP sent a new one, else `refresh_token` unchanged;
    it is not a verbatim echo of the IdP response.

    Args:
        params: Resolved OIDC parameters; `token_endpoint`, `client_id` and `scopes`.
        refresh_token: The stored refresh token to exchange.
        transport: Optional `httpx` transport that replaces the network (tests only).

    Returns:
        A `TokenResponse` with the new access token, its lifetime, and the refresh token
        to persist.

    Raises:
        CannotVerifyError: A network error or a 5xx response, so the credential was
            never judged.
        PsCliError: A 4xx response, an invalid JSON body or a malformed token response;
            the message says the stored credentials could not be refreshed and hints at
            `ps-cli auth login`. No `httpx` exception ever escapes.

    Side Effects:
        One HTTPS POST to `params.token_endpoint`.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": params.client_id,
        # Send the scopes (which already include `offline_access`) on every refresh,
        # not just the initial login, so a later refresh still gets a refresh token.
        "scope": " ".join(params.scopes),
    }
    with httpx.Client(
        timeout=_DEVICE_FLOW_TIMEOUT, transport=transport, verify=build_ssl_context()
    ) as client:
        try:
            response = client.post(params.token_endpoint, data=data)
        except httpx.HTTPError as exc:
            _raise_refresh_unverifiable(type(exc).__name__)
    if response.is_server_error:
        _raise_refresh_unverifiable(f"status {response.status_code}")
    if not response.is_success:
        _raise_refresh_failed()
    try:
        payload = response.json()
    except ValueError:
        _raise_refresh_failed()
    try:
        token_response = _parse_token_response(payload)
    except PsCliError:
        _raise_refresh_failed()
    new_refresh_token = (
        token_response.refresh_token if token_response.refresh_token is not None else refresh_token
    )
    return TokenResponse(
        access_token=token_response.access_token,
        refresh_token=new_refresh_token,
        expires_in=token_response.expires_in,
    )


def ensure_valid_access_token(
    *,
    context: str,
    service_url: str,
    auth_override: AuthOverrides | None,
    credential_store: CredentialStore,
    access_token_cache: AccessTokenCache,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Return a valid access token for `context`, refreshing it or failing closed.

    A cached token that is not about to expire is returned at once, with no store read
    and no network call. Otherwise the stored refresh token is exchanged at the IdP
    (parameters are re-resolved by discovery on every refresh), the possibly rotated
    refresh token is written back to the credential store, and the new access token
    and its expiry are put in `access_token_cache`. A long-running command therefore
    refreshes again once the token nears expiry; the check happens before a request is
    sent, never mid-request.

    Args:
        context: Name of the context whose credential is used.
        service_url: PS Service base URL, used for OIDC discovery.
        auth_override: The context's `auth` overrides, or `None`.
        credential_store: Where the refresh token is read from and rotated back to.
        access_token_cache: The invocation's in-memory cache; updated in place on a
            refresh.
        transport: Optional `httpx` transport that replaces the network for discovery
            and refresh (tests only).

    Returns:
        A non-empty access token valid for at least the expiry leeway.

    Raises:
        CredentialStoreError: The credential store cannot be read or written.
        CannotVerifyError: Discovery or the refresh could not reach the IdP or PS
            Service, or got a 5xx response.
        PsCliError: There are no stored credentials for `context`, the stored bundle
            has no refresh token, the refresh was rejected or malformed, or discovery
            failed; the hint is usually `ps-cli auth login`.

    Side Effects:
        On a cache miss: HTTPS requests to PS Service and the IdP, a credential-store
        write, and mutation of `access_token_cache`.
    """
    if access_token_cache.token is not None and not _is_cache_stale(access_token_cache):
        return access_token_cache.token

    bundle = credential_store.get_tokens(context)
    if bundle is None:
        _raise_no_stored_credentials(context)
    if bundle.refresh_token is None:
        _raise_refresh_failed()

    params = oidc_discovery.resolve_auth_parameters(service_url, auth_override, transport=transport)
    refreshed = _refresh_tokens(params, bundle.refresh_token, transport=transport)
    credential_store.set_tokens(
        context, TokenBundle(refresh_token=refreshed.refresh_token, issuer=params.issuer)
    )
    access_token_cache.token = refreshed.access_token
    access_token_cache.expires_at = int(time.time()) + refreshed.expires_in
    return access_token_cache.token
