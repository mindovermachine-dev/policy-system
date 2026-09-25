"""ps-cli RFC 8628 device-authorization flow: request, poll, and token conversion.

Issue #57 Slices 9-12. Three HTTP-shaped steps, kept I/O-free apart from the HTTP
calls themselves -- no `print()` anywhere in this module; Slice 13's
`handle_auth_login` does all user-facing printing, mirroring `PsServiceClient`'s own
split between fetch-and-parse methods and handlers' own `print()` calls
(`handlers.py` throughout):

- `request_device_authorization()` -- `POST <device_authorization_endpoint>`,
  mints a `DeviceAuthorization` (`device_code`/`user_code`/`verification_uri`/...).
- `poll_for_token()` -- hand-rolled polling loop against `<token_endpoint>`
  (no new dependency, per TASK.md's own Size note), honoring
  `authorization_pending`/`slow_down` (RFC 8628 SS3.5) and raising an actionable
  `PsCliError` on `expired_token`/`access_denied`/anything unrecognized.
- `complete_device_login()` -- wires the two together into the one call
  Slice 13's CLI handler makes.

Deviation from PLAN.md's literal `complete_device_login(...) -> DeviceAuthorization`
signature: that annotation contradicts PLAN.md's own prose two sentences later
("the resulting tokens, once converted, round-trip through
`token_bundle_from_response` correctly") -- a `DeviceAuthorization` alone carries no
tokens to round-trip. Returning only a `DeviceAuthorization` would also mean
`complete_device_login` never actually polls, making its name a lie. Instead,
`complete_device_login` takes an `on_device_authorization` callback, invoked with the
freshly-minted `DeviceAuthorization` *before* the (blocking) poll begins -- this is
exactly the seam Slice 13 needs to print `user_code`/`verification_uri` before the
poll blocks, without this module doing any printing itself, and without Slice 13
having to call `request_device_authorization`/`poll_for_token` separately. It
returns the `TokenResponse` the poll produced.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast

import httpx

from ps_cli import oidc_discovery
from ps_cli.credentials import TokenBundle
from ps_cli.errors import PsCliError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_cli.credentials import CredentialStore
    from ps_cli.oidc_discovery import ResolvedAuthParameters
    from ps_cli.targets import AuthOverrides

# Matches `oidc_discovery.py`'s own `_DISCOVERY_TIMEOUT` convention -- both the
# device-authorization request and each individual token-endpoint poll are small,
# fast JSON round-trips with no reason to wait any longer than PS Service's own fast
# endpoints (`http_client.py`'s own short, uniform connect/read/write/pool bound).
_DEVICE_FLOW_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)

_GRANT_TYPE_DEVICE_CODE = "urn:ietf:params:oauth:grant-type:device_code"

# RFC 8628 SS3.5's own recommended backoff increment on `slow_down`.
_SLOW_DOWN_INCREMENT_SECONDS = 5

_UNEXPECTED_DEVICE_AUTHORIZATION_SHAPE_MSG = (
    "the issuer returned an unexpected device-authorization response shape"
)
_UNEXPECTED_GRANT_RESPONSE_SHAPE_MSG = "the issuer returned an unexpected grant-response shape"

# AC-BI-012's clock-skew/in-flight-request buffer (issue #57 Slice 16, D-57 group 3),
# repurposed by issue #121 to gate the in-memory AccessTokenCache instead of a
# persisted TokenBundle's expires_at -- same buffer, same rationale (a request in
# flight must not race a token that expires mid-call), new target.
_EXPIRY_LEEWAY_SECONDS = 30

_RELOGIN_HINT = "run `ps-cli auth login`"
_REFRESH_FAILED_MSG = "stored credentials could not be refreshed"


@dataclass(frozen=True)
class DeviceAuthorization:
    """RFC 8628 SS3.2 device-authorization response: what Slice 13 prints for the user."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


@dataclass(frozen=True)
class TokenResponse:
    """The subset of RFC 8628 SS3.5's token-endpoint success response this issue needs."""

    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclass
class AccessTokenCache:
    """One CLI invocation's in-memory-only access token (AC-BI-004, issue #121).

    Never persisted. Constructed once per invocation by whichever object's lifetime *is*
    that invocation (`PsServiceClient.__init__` for a real ps-cli command;
    `mcp_bridge.main()` for the proxy-loop process), then passed explicitly into
    `ensure_valid_access_token`/`peek_cached_access_token` -- discarded at process exit by
    simply going out of scope, no explicit "clear" step needed.

    `expires_at` (issue #121 critical-flaw fix): the token's own absolute Unix-epoch
    expiry, set alongside `token` on every refresh. Never persisted, never read from any
    store -- exists purely so a long-lived holder of this cache (mcp_bridge's whole
    proxy-loop process) can detect its own token going stale mid-session and self-heal,
    instead of returning an increasingly-stale token forever once populated. For a real
    CLI invocation (a fresh process every time, `token` starts `None`), this field never
    changes AC-BI-003's "exactly one refresh per invocation" behavior: the first check
    always misses (`token is None`), so exactly one refresh still happens before the
    first business-endpoint call, same as before this field existed.
    """

    token: str | None = None
    expires_at: int | None = None


def _raise_connection_error(url: str, cause: BaseException) -> NoReturn:
    """Raise the actionable `PsCliError` for a connect failure to `url`.

    Duplicated from `oidc_discovery.py::_raise_connection_error` rather than
    imported -- this module talks to the IdP's device-authorization/token
    endpoints, a distinct logical resource from either of `oidc_discovery.py`'s two
    endpoints, so sharing one helper across modules would be the wrong layering
    (same rationale `oidc_discovery.py`'s own docstring gives for not going through
    `PsServiceClient`).
    """
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

    Defensive `isinstance`-chain parse, same style as `oidc_discovery.py`'s own
    parsers -- this document comes from a third-party IdP, never assumed to match
    the documented shape.
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
    """`POST params.device_authorization_endpoint` -- mint a fresh `DeviceAuthorization`.

    Sends `client_id` and `scope` (space-joined) always; sends `audience` only when
    `params.audience is not None` -- never as an empty string (AC-BI-003). Does no
    printing -- the caller (Slice 13) prints `user_code`/`verification_uri`.
    """
    data: dict[str, str] = {
        "client_id": params.client_id,
        "scope": " ".join(params.scopes),
    }
    if params.audience is not None:
        data["audience"] = params.audience
    with httpx.Client(timeout=_DEVICE_FLOW_TIMEOUT, transport=transport) as client:
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
    """Parse a `POST <token_endpoint>` 200 body into a `TokenResponse`."""
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

    Falls back to a synthetic `http-<status>` code (never raises/crashes here) when
    the body is not JSON or does not carry a string `error` field -- D5-style "never
    assume the server always returns the documented shape" (`http_client.py:502-511`).
    The caller (`_raise_for_error_code`) treats any code it does not recognize as
    generic, so this fallback still produces an actionable, non-crashing error.
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
    """Raise the AC-BI-009 `PsCliError` for a token-endpoint error code.

    `expired_token`/`access_denied` get the literal wording AC-BI-009 specifies;
    any other/unrecognized code (including this module's own `http-<status>`
    fallback) gets a generic `PsCliError` naming the raw code rather than crashing.
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
    """Poll `params.token_endpoint` until the device-flow login completes or fails.

    Hand-rolled loop (no new dependency) -- `interval` starts at `device_auth.interval`
    and grows by `_SLOW_DOWN_INCREMENT_SECONDS` on every `slow_down` response (RFC 8628
    SS3.5), resetting to no-longer-growing once the provider stops returning it.
    `sleep` is a constructor-injection seam (mirrors `handlers.py`'s
    `poll_interval_seconds` convention) so tests never wait on a real clock.
    """
    interval = device_auth.interval
    data = {
        "grant_type": _GRANT_TYPE_DEVICE_CODE,
        "device_code": device_auth.device_code,
        "client_id": params.client_id,
    }
    with httpx.Client(timeout=_DEVICE_FLOW_TIMEOUT, transport=transport) as client:
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

    `on_device_authorization`, when given, is called with the freshly-minted
    `DeviceAuthorization` *before* the (blocking) poll begins -- the seam Slice 13
    uses to print `user_code`/`verification_uri` at exactly the right moment, without
    this module doing any printing itself. See this module's own docstring for why
    this differs from PLAN.md's literal `-> DeviceAuthorization` return-type wording.
    """
    device_auth = request_device_authorization(params, transport=transport)
    if on_device_authorization is not None:
        on_device_authorization(device_auth)
    return poll_for_token(params, device_auth, transport=transport, sleep=sleep)


def token_bundle_from_response(response: TokenResponse, issuer: str) -> TokenBundle:
    """Convert a `TokenResponse` into the `TokenBundle` shape `CredentialStore` persists.

    Issue #121 (AC-BI-001): only `refresh_token`/`issuer` persist -- `response.
    access_token`/`expires_in` are never written to the store; the in-memory access
    token lives in `AccessTokenCache` instead, populated by `ensure_valid_access_token`,
    never here (this function is only ever called from `handle_auth_login`'s initial
    login path, which stores immediately after obtaining fresh tokens). Storing the
    result (`credential_store.set_tokens(...)`) is the caller's job -- this module
    never imports `credentials.py`'s concrete store classes, only the `TokenBundle`
    shape itself.
    """
    return TokenBundle(refresh_token=response.refresh_token, issuer=issuer)


def _raise_no_stored_credentials(context: str) -> NoReturn:
    """Raise the AC-BI-013 fail-closed `PsCliError` for a `context` with no stored bundle."""
    raise PsCliError(
        msg=f"no stored credentials for context '{context}'",
        hint=_RELOGIN_HINT,
    )


def _raise_refresh_failed() -> NoReturn:
    """Raise the AC-BI-013 fail-closed `PsCliError` for a refresh that could not complete.

    Shared by every "must re-login" outcome once a bundle is known to be expired:
    no refresh token at all, a network error, a non-2xx response, or a malformed
    response body -- AC-BI-013 treats all of these as one "you must re-login"
    outcome, with the same wording.
    """
    raise PsCliError(msg=_REFRESH_FAILED_MSG, hint=_RELOGIN_HINT)


def _is_cache_stale(cache: AccessTokenCache) -> bool:
    """Return whether `cache`'s token is expired or expiring within `_EXPIRY_LEEWAY_SECONDS`.

    Mirrors the old (pre-#121) `_is_expired(bundle: TokenBundle)` check bit-for-bit, but
    reads the in-memory `AccessTokenCache` instead of a persisted `TokenBundle` -- there is
    no persisted `expires_at` left to check (AC-BI-001), but the in-memory cache still
    needs one to self-heal within a long-lived process (issue #121 critical-flaw fix).
    `cache.expires_at is None` (nothing cached yet) counts as stale, so a fresh cache
    always falls through to a real refresh.
    """
    return cache.expires_at is None or cache.expires_at <= int(time.time()) + _EXPIRY_LEEWAY_SECONDS


def peek_cached_access_token(*, access_token_cache: AccessTokenCache) -> str | None:
    """Return this invocation's already-obtained access token, if any (D-121-3).

    Read-only: never calls the network, never writes the store, never raises --
    for a best-effort caller that must never trigger a refresh or a
    credential-store write. Contrast with `ensure_valid_access_token()`, which
    refreshes a stale/missing cache entry (and fails closed if it cannot); here,
    an empty cache simply means "attach no Authorization header", not an error.
    """
    return access_token_cache.token


def _refresh_tokens(
    params: ResolvedAuthParameters,
    refresh_token: str,
    *,
    transport: httpx.BaseTransport | None,
) -> TokenResponse:
    """`POST params.token_endpoint` with `grant_type=refresh_token`; return the refreshed tokens.

    Carries `refresh_token` forward unchanged if the response does not include a
    new one (AC-BI-005: "not every IdP rotates on every refresh"), else adopts the
    rotated one. Raises the AC-BI-009 fail-closed error (`_raise_refresh_failed`)
    on any network error, non-2xx response, or malformed body -- never lets an
    `httpx` exception or a raw `KeyError`/`TypeError` escape.

    Also sends `params.scopes` as `scope` (issue #119, AC-BI-003) -- `params` is
    freshly re-resolved by every `ensure_valid_access_token` call, so this already
    includes `offline_access`, keeping every refresh (not just the initial login)
    eligible for a new refresh_token in the response.

    Note: the `TokenResponse` this returns is not literally the IdP's token-endpoint
    response -- `.refresh_token` is already carry-forward-resolved (the old stored value
    when the endpoint didn't rotate it), narrower than `TokenResponse`'s own general
    docstring contract ("the subset of RFC 8628 SS3.5's token-endpoint success response").
    Callers should treat this specific return value as "the refresh_token to persist,"
    not as a verbatim echo of what the IdP sent.
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": params.client_id,
        # Issue #119, AC-BI-003: carry `params.scopes` (which already includes
        # `offline_access` -- see `oidc_discovery.resolve_auth_parameters`) forward on
        # every refresh, not just the initial login, so a *second* refresh still has a
        # refresh_token to work with.
        "scope": " ".join(params.scopes),
    }
    with httpx.Client(timeout=_DEVICE_FLOW_TIMEOUT, transport=transport) as client:
        try:
            response = client.post(params.token_endpoint, data=data)
        except httpx.HTTPError:
            _raise_refresh_failed()
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
    """Return a valid access token for `context`, refreshing (or failing closed) as needed.

    Cache-hit (`access_token_cache.token is not None` AND not `_is_cache_stale(...)`):
    returns immediately, no store read, no network call -- AC-BI-004's "reused by
    further business-endpoint calls within the same invocation."

    Cache-miss (nothing cached yet, OR the cached token is stale -- issue #121
    critical-flaw fix): reads the stored bundle, fails closed if absent
    (`_raise_no_stored_credentials`, AC-BI-002) or if `refresh_token is None`
    (`_raise_refresh_failed`), otherwise always refreshes (AC-BI-003, AC-BI-005's
    rotation preserved verbatim inside `_refresh_tokens`), persists
    `TokenBundle(refresh_token=refreshed.refresh_token, issuer=params.issuer)`,
    populates `access_token_cache.token`/`access_token_cache.expires_at`, and returns
    the new access token.

    For a real CLI invocation this is unobservably identical to a design with no
    `expires_at` field at all: the cache starts empty, so the first call always
    misses and refreshes exactly once, same as every subsequent call for the rest of
    that invocation once populated. For `mcp_bridge`'s long-lived proxy-loop process,
    this is what makes a real Entra ID access token's ~60-90 min TTL survive a session
    that outlives it: once `_is_cache_stale` trips, the very next forwarded message
    (not the one mid-flight -- this is checked before a request is sent, never
    mid-request) transparently re-refreshes and the process keeps working with no
    manual restart.

    `transport` is the constructor-injection seam tests use to substitute a
    combined real-provider/fake-metadata transport for `resolve_auth_parameters`'s
    own network calls and for the refresh POST itself -- production callers never
    pass it, so both go over the real network.
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
