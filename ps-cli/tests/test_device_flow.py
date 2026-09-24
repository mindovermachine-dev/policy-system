"""Tests for ps_cli.device_flow (issue #57 Slices 9-12; issue #121 critical-flaw fix).

Slices 9-11 drive the real `ps_test_support.mock_oidc_provider.MockOidcProvider`'s
device-authorization/token endpoints directly (no monkeypatching) -- this *is* the
IdP's own endpoint, mirroring `test_oidc_discovery.py`'s own Slice 6 convention.
The one unrecognized-error-code case (Slice 11) uses `httpx.MockTransport` instead,
since the real provider only ever emits the four documented RFC 8628 error codes.

Issue #121: `TokenBundle` shrinks to `refresh_token`/`issuer` only (AC-BI-001) --
every construction below drops `access_token`/`expires_at`. The in-memory-only
access token now lives in a required `access_token_cache: AccessTokenCache`
parameter threaded through `ensure_valid_access_token`/`peek_cached_access_token`
(D-121-2/D-121-3). CHANGES.md's critical-flaw fix (Appendix A1) keeps
`AccessTokenCache.expires_at` so a long-lived cache holder (e.g. `mcp_bridge`'s
proxy-loop process) can self-heal once its in-memory token goes stale, without
breaking AC-BI-003's "exactly one refresh per (fresh-process) invocation" for a
real CLI invocation, whose cache always starts empty.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli import oidc_discovery
from ps_cli.credentials import TokenBundle
from ps_cli.device_flow import (
    AccessTokenCache,
    DeviceAuthorization,
    TokenResponse,
    complete_device_login,
    ensure_valid_access_token,
    peek_cached_access_token,
    poll_for_token,
    request_device_authorization,
    token_bundle_from_response,
)
from ps_cli.errors import PsCliError
from ps_cli.oidc_discovery import ResolvedAuthParameters
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from ps_test_support.mock_oidc_provider import MockOidcProvider

_CLIENT_ID = "ps-cli-test-client"


def _params_for(
    provider: MockOidcProvider, *, audience: str | None = None
) -> ResolvedAuthParameters:
    """Build the `ResolvedAuthParameters` a real device-flow call against `provider` needs."""
    return ResolvedAuthParameters(
        issuer=provider.issuer,
        client_id=_CLIENT_ID,
        scopes=("openid",),
        audience=audience,
        device_authorization_endpoint=f"{provider.base_url}/device_authorization",
        token_endpoint=f"{provider.base_url}/token",
    )


def _fail_if_called(_: float) -> None:
    pytest.fail("sleep() should not have been called")


# --- Slice 9: request_device_authorization() ---------------------------------------


def test_request_device_authorization_happy_path_returns_provider_minted_fields(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """A well-formed 200 body parses into `DeviceAuthorization` field-for-field."""
    device_auth = request_device_authorization(_params_for(mock_oidc_provider))

    assert device_auth == DeviceAuthorization(
        device_code=device_auth.device_code,
        user_code=device_auth.user_code,
        verification_uri=f"{mock_oidc_provider.base_url}/device",
        verification_uri_complete=(
            f"{mock_oidc_provider.base_url}/device?user_code={device_auth.user_code}"
        ),
        expires_in=600,
        interval=1,
    )


def test_request_device_authorization_omits_audience_when_none(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """`params.audience=None` -> the provider recorded no `audience` key at all."""
    device_auth = request_device_authorization(_params_for(mock_oidc_provider, audience=None))

    state = mock_oidc_provider.device_flow_state(device_auth.device_code)
    assert state.audience is None


def test_request_device_authorization_sends_audience_when_set(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """`params.audience="ps-service"` -> the provider recorded exactly that value."""
    device_auth = request_device_authorization(
        _params_for(mock_oidc_provider, audience="ps-service")
    )

    state = mock_oidc_provider.device_flow_state(device_auth.device_code)
    assert state.audience == "ps-service"
    assert state.client_id == _CLIENT_ID


def test_request_device_authorization_malformed_body_raises_ps_cli_error() -> None:
    """A 200 body missing required fields raises `PsCliError`, not a `KeyError`."""

    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"device_code": "dc"})

    params = ResolvedAuthParameters(
        issuer="http://issuer.example",
        client_id=_CLIENT_ID,
        scopes=("openid",),
        audience=None,
        device_authorization_endpoint="http://issuer.example/device_authorization",
        token_endpoint="http://issuer.example/token",
    )

    with pytest.raises(PsCliError):
        request_device_authorization(params, transport=httpx.MockTransport(_handle))


# --- Slice 10: poll_for_token() (pending/slow_down) ---------------------------------


def test_poll_for_token_pending_then_provider_completes_returns_tokens(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """First poll is pending (sleeps with the provider's own interval); the second,
    after `complete_device_flow`, succeeds and returns the minted tokens.
    """
    params = _params_for(mock_oidc_provider)
    device_auth = request_device_authorization(params)
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        mock_oidc_provider.complete_device_flow(device_auth.device_code)

    response = poll_for_token(params, device_auth, sleep=fake_sleep)

    assert sleep_calls == [device_auth.interval]
    assert isinstance(response, TokenResponse)
    assert response.expires_in == 3600
    assert isinstance(response.refresh_token, str)


def test_poll_for_token_slow_down_increments_interval_once_then_stops_climbing(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """`simulate_slow_down_once` -> next sleep is `interval + 5`; the poll after that
    reverts to plain pending at the *same* (not further-increased) interval.
    """
    params = _params_for(mock_oidc_provider)
    device_auth = request_device_authorization(params)
    mock_oidc_provider.simulate_slow_down_once(device_auth.device_code)
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            mock_oidc_provider.complete_device_flow(device_auth.device_code)

    response = poll_for_token(params, device_auth, sleep=fake_sleep)

    assert sleep_calls == [device_auth.interval + 5, device_auth.interval + 5]
    assert isinstance(response, TokenResponse)


def test_poll_for_token_malformed_success_body_raises_ps_cli_error(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """A 200 body missing `access_token`/`expires_in` raises `PsCliError`, not `KeyError`."""

    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_type": "Bearer"})

    params = _params_for(mock_oidc_provider)
    device_auth = DeviceAuthorization(
        device_code="dc",
        user_code="uc",
        verification_uri=f"{mock_oidc_provider.base_url}/device",
        verification_uri_complete=None,
        expires_in=600,
        interval=1,
    )

    with pytest.raises(PsCliError):
        poll_for_token(
            params,
            device_auth,
            transport=httpx.MockTransport(_handle),
            sleep=_fail_if_called,
        )


# --- Slice 11: poll_for_token() (expired_token/access_denied/unrecognized) ----------


def test_poll_for_token_expired_device_code_raises_actionable_error(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    params = _params_for(mock_oidc_provider)
    device_auth = request_device_authorization(params)
    mock_oidc_provider.expire_device_code(device_auth.device_code)

    with pytest.raises(PsCliError) as excinfo:
        poll_for_token(params, device_auth, sleep=_fail_if_called)

    assert "expired" in excinfo.value.msg
    assert "ps-cli auth login" in (excinfo.value.hint or "")


def test_poll_for_token_denied_device_code_raises_actionable_error(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    params = _params_for(mock_oidc_provider)
    device_auth = request_device_authorization(params)
    mock_oidc_provider.deny_device_code(device_auth.device_code)

    with pytest.raises(PsCliError) as excinfo:
        poll_for_token(params, device_auth, sleep=_fail_if_called)

    assert "denied" in excinfo.value.msg
    assert "ps-cli auth login" in (excinfo.value.hint or "")


def test_poll_for_token_unrecognized_error_code_raises_generic_error_not_crash() -> None:
    """An error code none of the four documented ones -> generic `PsCliError`, no crash.

    Constructed via `httpx.MockTransport` directly -- the real `MockOidcProvider`
    only ever emits `authorization_pending`/`slow_down`/`expired_token`/`access_denied`.
    """

    def _handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "some_unrecognized_error"})

    params = ResolvedAuthParameters(
        issuer="http://issuer.example",
        client_id=_CLIENT_ID,
        scopes=("openid",),
        audience=None,
        device_authorization_endpoint="http://issuer.example/device_authorization",
        token_endpoint="http://issuer.example/token",
    )
    device_auth = DeviceAuthorization(
        device_code="dc",
        user_code="uc",
        verification_uri="http://issuer.example/device",
        verification_uri_complete=None,
        expires_in=600,
        interval=1,
    )

    with pytest.raises(PsCliError) as excinfo:
        poll_for_token(
            params,
            device_auth,
            transport=httpx.MockTransport(_handle),
            sleep=_fail_if_called,
        )

    assert "some_unrecognized_error" in excinfo.value.msg


# --- Slice 12: token_bundle_from_response() / complete_device_login() --------------


def test_token_bundle_from_response_persists_only_refresh_token_and_issuer() -> None:
    """AC-BI-001: `token_bundle_from_response` never carries `access_token`/`expires_at`
    into the persisted `TokenBundle` shape.
    """
    response = TokenResponse(access_token="at", refresh_token="rt", expires_in=3600)

    bundle = token_bundle_from_response(response, issuer="http://issuer.example")

    assert bundle == TokenBundle(refresh_token="rt", issuer="http://issuer.example")


def test_complete_device_login_full_flow_round_trips_via_real_provider(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """`complete_device_login` requests authorization, calls `on_device_authorization`
    *before* polling, and -- once `complete_device_flow` fires mid-poll -- returns a
    `TokenResponse` that round-trips through `token_bundle_from_response` correctly.
    """
    params = _params_for(mock_oidc_provider)
    printed: list[DeviceAuthorization] = []
    sleep_calls: list[float] = []

    def on_device_authorization(device_auth: DeviceAuthorization) -> None:
        printed.append(device_auth)

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        mock_oidc_provider.complete_device_flow(printed[0].device_code)

    response = complete_device_login(
        params, sleep=fake_sleep, on_device_authorization=on_device_authorization
    )

    # on_device_authorization fired before the (one) blocking poll.
    assert len(printed) == 1
    assert sleep_calls == [printed[0].interval]

    bundle = token_bundle_from_response(response, params.issuer)
    assert bundle.refresh_token == response.refresh_token
    assert bundle.issuer == mock_oidc_provider.issuer


# --- Issue #57 Group 3 (AC-BI-002/003/004/005/009): ensure_valid_access_token /
# peek_cached_access_token, retargeted onto AccessTokenCache by issue #121 -----------


class _FakeCredentialStore:
    """A minimal dict-backed `CredentialStore` double for this group's tests."""

    def __init__(self) -> None:
        """Start with no tokens stored for any context."""
        self._tokens: dict[str, TokenBundle] = {}

    def get_tokens(self, context: str) -> TokenBundle | None:
        """Return the stored `TokenBundle` for `context`, or `None` if none is stored."""
        return self._tokens.get(context)

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        """Store `tokens` for `context`, overwriting any existing value."""
        self._tokens[context] = tokens

    def delete_tokens(self, context: str) -> None:
        """Remove `context`'s stored token bundle; a no-op if none exists."""
        self._tokens.pop(context, None)


def _obtain_real_tokens(provider: MockOidcProvider) -> TokenResponse:
    """Run a full real device-flow login against `provider`; return the genuine tokens."""
    params = _params_for(provider)
    device_auth = request_device_authorization(params)
    provider.complete_device_flow(device_auth.device_code)
    return poll_for_token(params, device_auth, sleep=_fail_if_called)


def _patch_resolve_auth_parameters(
    monkeypatch: pytest.MonkeyPatch, provider: MockOidcProvider
) -> None:
    """Bypass PS-Service resource-metadata discovery entirely for these tests.

    `resolve_auth_parameters`'s own resolution logic is already covered by
    `test_oidc_discovery.py`; this group's tests exercise
    `ensure_valid_access_token`'s own refresh/fail-closed logic once parameters are
    known, so `service_url` here is never a real, reachable server -- only
    `provider`'s own real device-authorization/token endpoints are.
    """

    def _fake_resolve(
        service_url: str, override: object, *, transport: object = None
    ) -> ResolvedAuthParameters:
        del service_url, override, transport
        return _params_for(provider)

    monkeypatch.setattr(oidc_discovery, "resolve_auth_parameters", _fake_resolve)


class TestPeekCachedAccessToken:
    """D-121-3: read-only, in-memory-cache-only access-token lookup."""

    def test_returns_none_when_cache_is_empty(self) -> None:
        cache = AccessTokenCache()

        assert peek_cached_access_token(access_token_cache=cache) is None

    def test_returns_the_cached_token_verbatim_when_populated(self) -> None:
        cache = AccessTokenCache(token="fresh", expires_at=int(time.time()) + 3600)

        assert peek_cached_access_token(access_token_cache=cache) == "fresh"

    def test_returns_the_cached_token_even_when_stale(self) -> None:
        """A read-only peek never checks staleness -- that is `ensure_valid_access_token`'s
        job, not this best-effort function's (its own docstring: "never calls the
        network, never writes the store, never raises").
        """
        cache = AccessTokenCache(token="stale-but-cached", expires_at=int(time.time()) - 10)

        assert peek_cached_access_token(access_token_cache=cache) == "stale-but-cached"


class TestEnsureValidAccessToken:
    """AC-BI-002 (fail closed), AC-BI-003 (exactly one refresh per empty cache),
    AC-BI-004 (in-memory reuse), AC-BI-005 (rotation), plus issue #121's critical-flaw
    fix: a populated-but-stale cache still triggers exactly one more refresh.
    """

    def test_no_stored_credentials_raises_actionable_error(self) -> None:
        """AC-BI-002: no bundle at all -> the "no stored credentials" fail-closed error."""
        store = _FakeCredentialStore()

        with pytest.raises(PsCliError) as excinfo:
            ensure_valid_access_token(
                context="dev",
                service_url="http://ps-service.example",
                auth_override=None,
                credential_store=store,
                access_token_cache=AccessTokenCache(),
            )

        assert "no stored credentials for context 'dev'" in excinfo.value.msg
        assert "ps-cli auth login" in (excinfo.value.hint or "")

    def test_stored_bundle_with_no_refresh_token_raises_actionable_error(self) -> None:
        """AC-BI-002: a stored bundle with `refresh_token=None` -> "could not be refreshed"."""
        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token=None, issuer="http://issuer.example"))

        with pytest.raises(PsCliError) as excinfo:
            ensure_valid_access_token(
                context="dev",
                service_url="http://ps-service.example",
                auth_override=None,
                credential_store=store,
                access_token_cache=AccessTokenCache(),
            )

        assert excinfo.value.msg == "stored credentials could not be refreshed"
        assert "ps-cli auth login" in (excinfo.value.hint or "")

    def test_empty_cache_always_refreshes_regardless_of_how_long_the_bundle_has_been_stored(
        self, mock_oidc_provider: MockOidcProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-BI-003's literal "regardless of any prior token's expiry": there is no
        persisted expiry left to vary at all (AC-BI-001) -- an empty
        `AccessTokenCache` always calls the refresh endpoint on a cache miss, proven
        against the real provider so the returned access token is genuine, not just a
        return-value stand-in.
        """
        _patch_resolve_auth_parameters(monkeypatch, mock_oidc_provider)
        initial = _obtain_real_tokens(mock_oidc_provider)
        assert initial.refresh_token is not None
        store = _FakeCredentialStore()
        store.set_tokens(
            "dev",
            TokenBundle(refresh_token=initial.refresh_token, issuer=mock_oidc_provider.issuer),
        )

        token = ensure_valid_access_token(
            context="dev",
            service_url="http://ps-service.example",
            auth_override=None,
            credential_store=store,
            access_token_cache=AccessTokenCache(),
        )

        assert isinstance(token, str)
        assert token != ""

    def test_populated_fresh_cache_returns_cached_token_with_zero_further_transport_calls(
        self,
    ) -> None:
        """AC-BI-004: a pre-populated, still-fresh `AccessTokenCache` short-circuits --
        no store read, no network call. `service_url`/`credential_store` are both
        deliberately unusable (an unreachable host, a store that fails the test if
        touched) -- if the cache-hit path ever regressed into consulting either, this
        test would fail loudly instead of silently passing.
        """

        class _UncallableCredentialStore:
            def get_tokens(self, context: str) -> TokenBundle | None:
                pytest.fail(f"get_tokens must not be called, got context={context!r}")

            def set_tokens(self, context: str, tokens: TokenBundle) -> None:
                del tokens
                pytest.fail(f"set_tokens must not be called, got context={context!r}")

            def delete_tokens(self, context: str) -> None:
                pytest.fail(f"delete_tokens must not be called, got context={context!r}")

        cache = AccessTokenCache(token="already-cached", expires_at=int(time.time()) + 3600)

        token = ensure_valid_access_token(
            context="dev",
            service_url="http://ps-service-must-not-be-contacted.invalid",
            auth_override=None,
            credential_store=_UncallableCredentialStore(),
            access_token_cache=cache,
        )

        assert token == "already-cached"

    def test_populated_but_stale_cache_triggers_exactly_one_more_refresh(
        self, mock_oidc_provider: MockOidcProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #121 critical-flaw fix (CHANGES.md Appendix A1): a cache that already
        holds a token, but whose `expires_at` is in the past, is treated as a cache
        miss -- exactly one more refresh happens, and the cache is updated with the
        newly-refreshed token, not left holding the stale one. This is the proof that
        a long-lived cache holder (e.g. `mcp_bridge`'s proxy-loop process) can self-heal
        once its in-memory token goes stale, instead of returning an increasingly-stale
        token forever.
        """
        _patch_resolve_auth_parameters(monkeypatch, mock_oidc_provider)
        initial = _obtain_real_tokens(mock_oidc_provider)
        assert initial.refresh_token is not None
        store = _FakeCredentialStore()
        store.set_tokens(
            "dev",
            TokenBundle(refresh_token=initial.refresh_token, issuer=mock_oidc_provider.issuer),
        )
        stale_cache = AccessTokenCache(token="stale-token", expires_at=int(time.time()) - 10)

        new_token = ensure_valid_access_token(
            context="dev",
            service_url="http://ps-service.example",
            auth_override=None,
            credential_store=store,
            access_token_cache=stale_cache,
        )

        assert new_token != "stale-token"
        assert stale_cache.token == new_token
        assert stale_cache.expires_at is not None
        assert stale_cache.expires_at > int(time.time())
        # The store afterward holds the *rotated* refresh_token, not the original --
        # proving the refresh (and the store write) actually happened, not just a
        # cache-internal mutation.
        rotated = store.get_tokens("dev")
        assert rotated is not None
        assert rotated.refresh_token != initial.refresh_token

    def test_expired_bundle_with_valid_refresh_token_refreshes_and_rotates_store(
        self, mock_oidc_provider: MockOidcProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-BI-005: a cache-miss refresh yields a *new* access_token, and the store
        afterward holds the *rotated* refresh_token, not the original.
        """
        _patch_resolve_auth_parameters(monkeypatch, mock_oidc_provider)
        initial = _obtain_real_tokens(mock_oidc_provider)
        assert initial.refresh_token is not None
        store = _FakeCredentialStore()
        store.set_tokens(
            "dev",
            TokenBundle(refresh_token=initial.refresh_token, issuer=mock_oidc_provider.issuer),
        )

        new_token = ensure_valid_access_token(
            context="dev",
            service_url="http://ps-service.example",
            auth_override=None,
            credential_store=store,
            access_token_cache=AccessTokenCache(),
        )

        rotated = store.get_tokens("dev")
        assert rotated is not None
        assert rotated.refresh_token != initial.refresh_token
        assert isinstance(new_token, str)
        assert new_token != ""

    def test_refresh_request_carries_forward_the_resolved_scope(
        self, mock_oidc_provider: MockOidcProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #119, AC-BI-003: the refresh-token POST sends `params.scopes` as `scope`
        -- proven with `offline_access` in the mix, the scope this fix cares about -- not
        just the initial device-flow request. Without this, a bundle refreshed once would
        still lose its refresh_token on the *second* refresh.
        """
        params = ResolvedAuthParameters(
            issuer=mock_oidc_provider.issuer,
            client_id=_CLIENT_ID,
            scopes=("openid", "offline_access"),
            audience=None,
            device_authorization_endpoint=f"{mock_oidc_provider.base_url}/device_authorization",
            token_endpoint=f"{mock_oidc_provider.base_url}/token",
        )
        device_auth = request_device_authorization(params)
        mock_oidc_provider.complete_device_flow(device_auth.device_code)
        initial = poll_for_token(params, device_auth, sleep=_fail_if_called)
        assert initial.refresh_token is not None
        store = _FakeCredentialStore()
        store.set_tokens(
            "dev",
            TokenBundle(refresh_token=initial.refresh_token, issuer=mock_oidc_provider.issuer),
        )

        def _fake_resolve(
            service_url: str, override: object, *, transport: object = None
        ) -> ResolvedAuthParameters:
            del service_url, override, transport
            return params

        monkeypatch.setattr(oidc_discovery, "resolve_auth_parameters", _fake_resolve)

        ensure_valid_access_token(
            context="dev",
            service_url="http://ps-service.example",
            auth_override=None,
            credential_store=store,
            access_token_cache=AccessTokenCache(),
        )

        sent_scope = mock_oidc_provider.last_token_request_form.get("scope")
        assert sent_scope is not None
        assert "offline_access" in sent_scope.split()

    def test_refresh_rejected_for_unrecognized_scope_fails_closed_like_any_refresh_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #119, AC-BI-004: an IdP that rejects the newly-added `offline_access`
        scope (e.g. `invalid_scope`) is handled by the existing generic refresh-failure
        path -- no special-casing added, no new crash mode.
        """
        params = ResolvedAuthParameters(
            issuer="http://issuer.example",
            client_id=_CLIENT_ID,
            scopes=("openid", "offline_access"),
            audience=None,
            device_authorization_endpoint="http://issuer.example/device_authorization",
            token_endpoint="http://issuer.example/token",
        )

        def _fake_resolve(
            service_url: str, override: object, *, transport: object = None
        ) -> ResolvedAuthParameters:
            del service_url, override, transport
            return params

        monkeypatch.setattr(oidc_discovery, "resolve_auth_parameters", _fake_resolve)

        def _handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_scope"})

        store = _FakeCredentialStore()
        store.set_tokens("dev", TokenBundle(refresh_token="rt", issuer="http://issuer.example"))

        with pytest.raises(PsCliError) as excinfo:
            ensure_valid_access_token(
                context="dev",
                service_url="http://ps-service.example",
                auth_override=None,
                credential_store=store,
                access_token_cache=AccessTokenCache(),
                transport=httpx.MockTransport(_handle),
            )

        assert excinfo.value.msg == "stored credentials could not be refreshed"
        assert "ps-cli auth login" in (excinfo.value.hint or "")

    def test_second_call_with_now_stale_refresh_token_raises_fail_closed_not_a_crash(
        self, mock_oidc_provider: MockOidcProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates a second process racing on an already-rotated refresh_token: the
        provider's own "already rotated" `invalid_grant` rejection surfaces as
        AC-BI-002's fail-closed error, not a crash.
        """
        _patch_resolve_auth_parameters(monkeypatch, mock_oidc_provider)
        initial = _obtain_real_tokens(mock_oidc_provider)
        assert initial.refresh_token is not None
        store = _FakeCredentialStore()

        def _seed_with(refresh_token: str) -> None:
            store.set_tokens(
                "dev", TokenBundle(refresh_token=refresh_token, issuer=mock_oidc_provider.issuer)
            )

        _seed_with(initial.refresh_token)
        # First call rotates the refresh_token; the original is now stale.
        ensure_valid_access_token(
            context="dev",
            service_url="http://ps-service.example",
            auth_override=None,
            credential_store=store,
            access_token_cache=AccessTokenCache(),
        )
        # A second, racing process still holds the now-stale original token -- and its
        # own, separate (empty) AccessTokenCache, since it is a different invocation.
        _seed_with(initial.refresh_token)

        with pytest.raises(PsCliError) as excinfo:
            ensure_valid_access_token(
                context="dev",
                service_url="http://ps-service.example",
                auth_override=None,
                credential_store=store,
                access_token_cache=AccessTokenCache(),
            )

        assert excinfo.value.msg == "stored credentials could not be refreshed"
        assert "ps-cli auth login" in (excinfo.value.hint or "")


# --- Slice 22: AC-BI-018 never log a token value ------------------------------------

_MARKER_REFRESH_TOKEN = "marker-refresh-token-should-never-print-79c3"


def test_device_flow_never_prints_a_token_value_on_any_error_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proof pass over every `PsCliError` raised across Slices 10/11
    (`poll_for_token`'s own error paths) and 16/17 (`ensure_valid_access_token`'s
    refresh path), with a distinctive marker value deliberately in scope when each
    is raised, checked for in `.msg`/`.hint` (AC-BI-018). No production-code change
    expected -- this is a proof pass over Slice 1's actual code.
    """
    params = ResolvedAuthParameters(
        issuer="http://issuer.example",
        client_id=_CLIENT_ID,
        scopes=("openid",),
        audience=None,
        device_authorization_endpoint="http://issuer.example/device_authorization",
        token_endpoint="http://issuer.example/token",
    )
    # Slices 10/11: no access/refresh token exists yet at this point in the flow --
    # `device_code` is the one secret-like value in scope, so it stands in for
    # "a token value" here.
    device_auth = DeviceAuthorization(
        device_code="dc-marker-should-never-print-79c3",
        user_code="uc",
        verification_uri="http://issuer.example/device",
        verification_uri_complete=None,
        expires_in=600,
        interval=1,
    )

    def _assert_poll_error_never_leaks_device_code(error_code: str) -> None:
        def _handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(400, json={"error": error_code})

        with pytest.raises(PsCliError) as excinfo:
            poll_for_token(
                params,
                device_auth,
                transport=httpx.MockTransport(_handle),
                sleep=_fail_if_called,
            )
        assert device_auth.device_code not in excinfo.value.msg
        assert device_auth.device_code not in (excinfo.value.hint or "")

    _assert_poll_error_never_leaks_device_code("expired_token")
    _assert_poll_error_never_leaks_device_code("access_denied")
    _assert_poll_error_never_leaks_device_code("some_unrecognized_error")

    # Slices 16/17: ensure_valid_access_token's refresh path, with a marker
    # refresh token already in scope when each failure is raised.
    def _fake_resolve(
        service_url: str, override: object, *, transport: object = None
    ) -> ResolvedAuthParameters:
        del service_url, override, transport
        return params

    monkeypatch.setattr(oidc_discovery, "resolve_auth_parameters", _fake_resolve)

    def _assert_refresh_failure_never_leaks_marker(transport: httpx.BaseTransport) -> None:
        store = _FakeCredentialStore()
        store.set_tokens(
            "dev",
            TokenBundle(refresh_token=_MARKER_REFRESH_TOKEN, issuer="http://issuer.example"),
        )

        with pytest.raises(PsCliError) as excinfo:
            ensure_valid_access_token(
                context="dev",
                service_url="http://ps-service.example",
                auth_override=None,
                credential_store=store,
                access_token_cache=AccessTokenCache(),
                transport=transport,
            )
        assert _MARKER_REFRESH_TOKEN not in excinfo.value.msg
        assert _MARKER_REFRESH_TOKEN not in (excinfo.value.hint or "")

    def _connect_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _assert_refresh_failure_never_leaks_marker(httpx.MockTransport(_connect_error_handler))

    def _non_2xx_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, json={"error": "invalid_grant"})

    _assert_refresh_failure_never_leaks_marker(httpx.MockTransport(_non_2xx_handler))

    def _malformed_json_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"not json")

    _assert_refresh_failure_never_leaks_marker(httpx.MockTransport(_malformed_json_handler))

    def _malformed_shape_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"token_type": "Bearer"})

    _assert_refresh_failure_never_leaks_marker(httpx.MockTransport(_malformed_shape_handler))
