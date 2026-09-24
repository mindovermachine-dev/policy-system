"""Capstone IdP-agnostic verification test (issue #57 Slice 23, AC-BI-019).

Exercises the full `auth login` -> authenticated business call -> silent
refresh -> `auth logout` cycle end to end against a real, generic
`ps_test_support.mock_oidc_provider.MockOidcProvider` -- no mocking anywhere
below the HTTP boundary this issue's own code actually reaches over the
network.

Two distinct fakes are needed, for two distinct reasons:

- PS Service's own `/.well-known/oauth-protected-resource` endpoint must be a
  real, local, loopback `http.server.HTTPServer` (mirroring
  `test_auth_handlers.py::_FakeJsonServer`) -- `auth_handlers.handle_auth_login`
  calls `oidc_discovery.resolve_auth_parameters(config.service_url,
  auth_override)` with no `transport` seam at all (neither its own dispatch
  adapter nor `handle_auth_login` itself ever thread one through), so this
  half of the flow has no injection point other than a real reachable address.
- `PsServiceClient`'s own calls *do* accept a `transport` constructor argument
  (issue #121's `PsServiceClient.__init__` threads it into
  `ensure_valid_access_token` too, not just business calls -- see
  `_build_ps_service_transport`'s own docstring), so those are served by a
  genuine `httpx.MockTransport` that: enforces the bearer requirement on the
  business route for real (200 with a `Bearer` header attached, 401 without,
  proven inline so the later "succeeds with the token attached" assertion is
  meaningful); answers PS Service's own resource-metadata and the IdP's
  `openid-configuration` documents so `resolve_auth_parameters` resolves
  without a real network hop; and, for the `grant_type=refresh_token` case,
  delegates to the real `mock_oidc_provider.handle_token_request(...)` --
  the actual provider state machine (rotation, `invalid_grant` rejection),
  not a hand-rolled static response.

`handle_auth_login` is called directly, not via `ps_cli.cli.run(["auth",
"login", ...])`: `AUTH_DISPATCH`'s dispatch adapter always uses the real
`time.sleep`, with no seam to fake it, and this test must never block on a
real clock waiting for `MockOidcProvider`'s device-flow poll interval.
`config set-context`/`auth logout`/`auth status` have no such constraint, so
those go through the real `run()` CLI entry point.

Issue #121, Slice 4 (TASK.md Deliverables: "verify refresh-token rotation
(#57 AC-BI-012) and fail-closed-on-refresh-failure (#57 AC-BI-013) still hold
once persisted shape drops access_token/expires_at"): `TokenBundle` no longer
has an `expires_at` to force expired, so "force a refresh" is now proven the
way AC-BI-003 actually works post-#121 -- a brand new `PsServiceClient`
(D-121-2's invocation boundary is one `PsServiceClient` instance) starts with
an empty `AccessTokenCache` and therefore always refreshes on its first
authenticated call, `regardless of any prior token's expiry`. Two tests: the
full cycle below, and a dedicated fail-closed proof
(`test_refresh_token_rejected_by_mock_oidc_provider_surfaces_actionable_relogin_error`)
that a refresh_token the real provider rejects surfaces the existing
"run `ps-cli auth login`" `PsCliError`, not a raw HTTP/OAuth error.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import pytest

from ps_cli import device_flow
from ps_cli.cli import run
from ps_cli.config import load_config
from ps_cli.credentials import TokenBundle, build_credential_store
from ps_cli.errors import PsCliError
from ps_cli.http_client import PsServiceClient
from ps_cli.modules.auth_handlers import handle_auth_login
from ps_cli.targets import load_targets, resolve_config_dir
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import InMemoryKeyringBackend

    from ps_cli.credentials import CredentialStore
    from ps_cli.device_flow import DeviceAuthorization
    from ps_cli.oidc_discovery import ResolvedAuthParameters
    from ps_cli.targets import AuthOverrides
    from ps_test_support.mock_oidc_provider import MockOidcProvider

_CLIENT_ID = "ps-cli-integration-test-client"
_CONTEXT_NAME = "test"
_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
_OPENID_CONFIGURATION_PATH = "/.well-known/openid-configuration"
_NEAR_MISSES_PATH = "/near-misses"


def _build_resource_metadata_handler(body: dict[str, object]) -> type[BaseHTTPRequestHandler]:
    """Build a handler class serving `body` at `_RESOURCE_METADATA_PATH`, 404 elsewhere.

    Closure-based factory -- `HTTPServer` requires a handler *class*, not an
    instance, so `body` must be captured some way other than `self`. Mirrors
    `test_auth_handlers.py::_build_fake_json_handler`'s own recipe.
    """

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Silence `BaseHTTPRequestHandler`'s default stderr access log."""

        def do_GET(self) -> None:
            if self.path == _RESOURCE_METADATA_PATH:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

    return _Handler


class _FakePsServiceMetadataServer:
    """A real, ephemeral-port local server serving PS Service's own resource metadata.

    Required (see this module's own docstring) because `resolve_auth_parameters`
    fetches it over a real, un-injectable `httpx.Client` on the `auth login` path.
    """

    def __init__(self, body: dict[str, object]) -> None:
        """Start serving `body` at `_RESOURCE_METADATA_PATH` on an ephemeral loopback port."""
        self._server = HTTPServer(("127.0.0.1", 0), _build_resource_metadata_handler(body))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        """This server's own base URL, e.g. `http://127.0.0.1:54321`."""
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def shutdown(self) -> None:
        """Stop the background HTTP server thread."""
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def fake_ps_service_metadata_server(
    mock_oidc_provider: MockOidcProvider,
) -> Iterator[_FakePsServiceMetadataServer]:
    """A real local server serving resource metadata that names `mock_oidc_provider`
    as the one authorization server, shut down afterward.
    """
    server = _FakePsServiceMetadataServer(
        {
            "resource": "http://ps-service.example",
            "authorization_servers": [mock_oidc_provider.issuer],
            "scopes_supported": ["openid"],
            "ps_cli_client_id": _CLIENT_ID,
        }
    )
    try:
        yield server
    finally:
        server.shutdown()


def _decode_form_body(request: httpx.Request) -> dict[str, str]:
    """Form-decode `request.content` the same way `mock_oidc_provider.py`'s own
    `_read_form_body` decodes a real `POST` body -- duplicated rather than imported
    since that helper reads off a `BaseHTTPRequestHandler`, not an `httpx.Request`.
    """
    parsed = parse_qs(request.content.decode("utf-8"))
    return {key: values[0] for key, values in parsed.items() if values}


def _build_ps_service_transport(
    mock_oidc_provider: MockOidcProvider,
) -> tuple[httpx.MockTransport, list[str]]:
    """A fake PS Service transport a `PsServiceClient` can be constructed with.

    Answers three distinct logical endpoints, since issue #121's `PsServiceClient`
    threads its one `transport` constructor argument into both
    `ensure_valid_access_token` (resource-metadata/openid-configuration/refresh) and
    its own business calls (`http_client.py`'s `self._transport`, Slice 1's
    deviation #1) -- a transport that only answered the business route would 404 the
    auth-resolution step before ever reaching it:

    - `_RESOURCE_METADATA_PATH` -- PS Service's own resource metadata, naming
      `mock_oidc_provider` as the one authorization server.
    - `_OPENID_CONFIGURATION_PATH` -- proxied verbatim from
      `mock_oidc_provider.discovery_document()`.
    - `mock_oidc_provider`'s own `/token` path, for `grant_type=refresh_token`
      requests -- delegated to the real, bound `mock_oidc_provider.
      handle_token_request(...)`, so a refresh through this transport exercises the
      provider's genuine rotation/`invalid_grant` state machine, never a
      hand-constructed fake response (Slice 4, TASK.md Deliverables).
    - `_NEAR_MISSES_PATH` -- the one business route this suite calls, genuinely
      enforcing the bearer requirement (200 with an empty review list given a
      non-empty `Bearer` token, 401 without) so a later "succeeds with the token
      attached" assertion is meaningful, not trivially true.

    Returns `(transport, refresh_grant_tokens)`: `refresh_grant_tokens` records the
    presented `refresh_token` value of every `grant_type=refresh_token` request this
    transport receives, in call order -- letting a test assert both how many
    refreshes happened and that each one presented a distinct (rotated) token,
    without a separate monkeypatched spy around the provider.
    """
    refresh_grant_tokens: list[str] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == _RESOURCE_METADATA_PATH:
            return httpx.Response(
                200,
                json={
                    "resource": "http://ps-service.example",
                    "authorization_servers": [mock_oidc_provider.issuer],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": _CLIENT_ID,
                },
            )
        if request.url.path == _OPENID_CONFIGURATION_PATH:
            return httpx.Response(200, json=mock_oidc_provider.discovery_document())
        if request.url.path == "/token":
            form = _decode_form_body(request)
            if form.get("grant_type") == "refresh_token":
                refresh_grant_tokens.append(form.get("refresh_token", ""))
            status, body = mock_oidc_provider.handle_token_request(form)
            return httpx.Response(status, json=body)
        if request.url.path != _NEAR_MISSES_PATH:
            return httpx.Response(404)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer ") or not auth_header.removeprefix("Bearer "):
            return httpx.Response(
                401,
                json={"error": {"code": "unauthenticated", "message": "missing bearer token"}},
            )
        return httpx.Response(200, json={"reviews": []})

    return httpx.MockTransport(_handle), refresh_grant_tokens


def _set_context_and_log_in(
    mock_oidc_provider: MockOidcProvider,
    fake_ps_service_metadata_server: _FakePsServiceMetadataServer,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AuthOverrides | None, CredentialStore, str]:
    """`config set-context` + a real device-flow `auth login`.

    Shared by every test in this module that needs a genuinely-issued, stored
    refresh_token to start from -- `handle_auth_login` is called directly, not via
    `run()`, for the reason this module's own docstring gives. Returns
    `(auth_override, credential_store, device_code)`; `device_code` lets a caller
    read back `mock_oidc_provider.device_flow_state(...)` (e.g. to prove audience
    passthrough), and `config_dir`/`_CONTEXT_NAME` are not threaded back out since
    every test in this module targets the same fixed context name.
    """
    config_dir = resolve_config_dir()

    set_context_exit_code = run(
        [
            "config",
            "set-context",
            _CONTEXT_NAME,
            "--url",
            fake_ps_service_metadata_server.base_url,
            "--auth-audience",
            "ps-service",
        ]
    )
    assert set_context_exit_code == 0

    # `sleep` faked to approve the device code on its first invocation (mirrors
    # `test_auth_handlers.py`'s own established convention for this exact seam).
    captured_device_auth: list[DeviceAuthorization] = []
    original_request_device_authorization = device_flow.request_device_authorization

    def _spy_request_device_authorization(
        params: ResolvedAuthParameters, *, transport: httpx.BaseTransport | None = None
    ) -> DeviceAuthorization:
        result = original_request_device_authorization(params, transport=transport)
        captured_device_auth.append(result)
        return result

    monkeypatch.setattr(
        device_flow, "request_device_authorization", _spy_request_device_authorization
    )

    def _fake_sleep(seconds: float) -> None:
        del seconds
        mock_oidc_provider.complete_device_flow(captured_device_auth[0].device_code)

    config = load_config(context=_CONTEXT_NAME, config_dir=config_dir)
    targets = load_targets(config_dir)
    assert targets is not None
    auth_override = targets.contexts[_CONTEXT_NAME].auth
    credential_store = build_credential_store()

    handle_auth_login(
        config.context_name,
        config,
        config_dir=config_dir,
        credential_store=credential_store,
        auth_override=auth_override,
        sleep=_fake_sleep,
    )

    return auth_override, credential_store, captured_device_auth[0].device_code


@pytest.mark.integration
def test_full_login_call_refresh_logout_cycle_against_generic_mock_oidc_provider(
    mock_oidc_provider: MockOidcProvider,
    fake_ps_service_metadata_server: _FakePsServiceMetadataServer,
    portable_keyring: InMemoryKeyringBackend,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-019: log in, make authenticated calls across several invocations, log out.

    Issue #121, D-121-7: `portable_keyring` (`conftest.py`) replaces the old
    `_force_no_keyring`-then-fall-back-to-file mechanism -- there is no file fallback
    left to fall back to (AC-BI-008); this fixture just gives `build_credential_
    store()`'s real, zero-argument production wiring a portable, working in-memory
    keyring so this test never depends on a real OS keyring backend being present.
    Requested for its monkeypatching side effect only -- never read directly.

    Issue #121, Slice 4: AC-BI-003 ("exactly one refresh-token exchange... before its
    first business-endpoint call") and AC-BI-004 ("reused... within the same
    invocation") are proven at their real granularity -- one `PsServiceClient`
    instance is D-121-2's invocation boundary, not a persisted bundle's expiry, which
    no longer exists (AC-BI-001). `refresh_grant_tokens` (from
    `_build_ps_service_transport`) is asserted after every step below, so a
    regression that refreshes zero, or more than once, per invocation boundary fails
    loudly rather than merely returning the right business-call result by accident.
    """
    auth_override, credential_store, device_code = _set_context_and_log_in(
        mock_oidc_provider, fake_ps_service_metadata_server, monkeypatch
    )

    login_output = capsys.readouterr().out
    assert f"logged in to {_CONTEXT_NAME} ({mock_oidc_provider.issuer})" in login_output
    recorded_state = mock_oidc_provider.device_flow_state(device_code)
    assert recorded_state.audience == "ps-service"  # AC-BI-003's audience passthrough

    transport, refresh_grant_tokens = _build_ps_service_transport(mock_oidc_provider)

    # --- An unauthenticated client's business call is rejected -- confirmed before
    # the "succeeds with the token attached" assertions below, so those are
    # meaningful, not trivially true.
    unauthenticated_client = PsServiceClient(
        fake_ps_service_metadata_server.base_url, transport=transport
    )
    with pytest.raises(PsCliError) as no_token_excinfo:
        unauthenticated_client.list_pending_reviews()
    assert "authentication rejected" in no_token_excinfo.value.msg
    assert refresh_grant_tokens == []  # never even attempted a refresh

    # --- Invocation 1: a fresh `PsServiceClient` -> empty `AccessTokenCache` ->
    # its first authenticated call always refreshes (AC-BI-003), regardless of how
    # long the stored refresh_token has existed -- there is no expiry left to check.
    first_invocation_client = PsServiceClient(
        fake_ps_service_metadata_server.base_url,
        transport=transport,
        credential_store=credential_store,
        context=_CONTEXT_NAME,
        auth_override=auth_override,
    )
    result = first_invocation_client.list_pending_reviews()
    assert result.reviews == []
    assert len(refresh_grant_tokens) == 1

    # --- A second call on the *same* `PsServiceClient` instance (same invocation)
    # reuses the in-memory `AccessTokenCache` -- zero further refreshes (AC-BI-004).
    result_again = first_invocation_client.list_pending_reviews()
    assert result_again.reviews == []
    assert len(refresh_grant_tokens) == 1

    stored_after_first_invocation = credential_store.get_tokens(_CONTEXT_NAME)
    assert stored_after_first_invocation is not None
    rotated_once = stored_after_first_invocation.refresh_token
    assert rotated_once is not None
    assert rotated_once != refresh_grant_tokens[0]  # AC-BI-005: rotated, not reused

    # --- Invocation 2: a brand new `PsServiceClient` (fresh `AccessTokenCache`) is
    # the realistic "next ps-cli invocation" boundary -- its first call refreshes
    # again, presenting the *rotated* refresh_token invocation 1 left behind.
    second_invocation_client = PsServiceClient(
        fake_ps_service_metadata_server.base_url,
        transport=transport,
        credential_store=credential_store,
        context=_CONTEXT_NAME,
        auth_override=auth_override,
    )
    result_second_invocation = second_invocation_client.list_pending_reviews()
    assert result_second_invocation.reviews == []
    assert len(refresh_grant_tokens) == 2
    assert refresh_grant_tokens[1] == rotated_once

    stored_after_second_invocation = credential_store.get_tokens(_CONTEXT_NAME)
    assert stored_after_second_invocation is not None
    assert stored_after_second_invocation.refresh_token != rotated_once

    # --- `auth logout` -- the next invocation's business call now fails closed
    # (AC-BI-002), and `auth status` reports "not logged in".
    logout_exit_code = run(["auth", "logout", "--context", _CONTEXT_NAME])
    assert logout_exit_code == 0

    # A fresh `PsServiceClient` (fresh cache) is required here too: `first_invocation_
    # client`'s own cache is still fresh (well within its 3600s lifetime) and would
    # otherwise short-circuit on the cache-hit path without ever consulting the now-
    # empty store, silently passing for the wrong reason.
    post_logout_client = PsServiceClient(
        fake_ps_service_metadata_server.base_url,
        transport=transport,
        credential_store=credential_store,
        context=_CONTEXT_NAME,
        auth_override=auth_override,
    )
    with pytest.raises(PsCliError) as logged_out_excinfo:
        post_logout_client.list_pending_reviews()
    assert "no stored credentials" in logged_out_excinfo.value.msg
    assert "ps-cli auth login" in (logged_out_excinfo.value.hint or "")
    assert len(refresh_grant_tokens) == 2  # fails closed before ever attempting one

    status_exit_code = run(["auth", "status", "--context", _CONTEXT_NAME])
    status_output = capsys.readouterr().out
    assert status_exit_code == 0
    assert f"not logged in to '{_CONTEXT_NAME}'" in status_output


@pytest.mark.integration
def test_refresh_token_rejected_by_mock_oidc_provider_surfaces_actionable_relogin_error(
    mock_oidc_provider: MockOidcProvider,
    fake_ps_service_metadata_server: _FakePsServiceMetadataServer,
    portable_keyring: InMemoryKeyringBackend,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #121 Slice 4 dedicated fail-closed proof (TASK.md Deliverables item,
    AC-BI-009 -- issue #57's former AC-BI-013 wording).

    A refresh_token the real `MockOidcProvider` rejects (never issued -- the
    provider's own `_handle_refresh_token_grant` returns a genuine
    `400 {"error": "invalid_grant"}`, not a hand-constructed fake response) surfaces
    as the existing "run `ps-cli auth login`" `PsCliError`, confirmed against the
    real `_refresh_tokens` parsing path: `refresh_grant_tokens` proves exactly one
    refresh attempt was made (and rejected), not skipped or retried.
    """
    del portable_keyring
    auth_override, credential_store, _device_code = _set_context_and_log_in(
        mock_oidc_provider, fake_ps_service_metadata_server, monkeypatch
    )
    capsys.readouterr()  # discard the login confirmation printed to stdout

    stored = credential_store.get_tokens(_CONTEXT_NAME)
    assert stored is not None
    credential_store.set_tokens(
        _CONTEXT_NAME,
        TokenBundle(refresh_token="never-issued-refresh-token-1a2b3c", issuer=stored.issuer),
    )

    transport, refresh_grant_tokens = _build_ps_service_transport(mock_oidc_provider)
    client = PsServiceClient(
        fake_ps_service_metadata_server.base_url,
        transport=transport,
        credential_store=credential_store,
        context=_CONTEXT_NAME,
        auth_override=auth_override,
    )

    with pytest.raises(PsCliError) as excinfo:
        client.list_pending_reviews()

    assert excinfo.value.msg == "stored credentials could not be refreshed"
    assert "ps-cli auth login" in (excinfo.value.hint or "")
    assert refresh_grant_tokens == ["never-issued-refresh-token-1a2b3c"]
