"""Capstone IdP-agnostic verification test (issue #57 Slice 23, AC-BI-019).

One test, exercising the full `auth login` -> authenticated business call ->
silent refresh -> `auth logout` cycle end to end against a real, generic
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
- PS Service's business endpoints (`PsServiceClient`'s own calls) *do* accept
  a `transport` constructor argument, so those are served by a genuine
  `httpx.MockTransport` that enforces the bearer requirement for real (200
  with a `Bearer` header attached, 401 without) -- proven inline, so the
  later "succeeds with the token attached" assertion is meaningful, not
  trivially true.

`handle_auth_login` is called directly, not via `ps_cli.cli.run(["auth",
"login", ...])`: `AUTH_DISPATCH`'s dispatch adapter always uses the real
`time.sleep`, with no seam to fake it, and this test must never block on a
real clock waiting for `MockOidcProvider`'s device-flow poll interval.
`config set-context`/`auth logout`/`auth status` have no such constraint, so
those go through the real `run()` CLI entry point.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import httpx
import keyring.errors
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

    from ps_cli.device_flow import DeviceAuthorization
    from ps_cli.oidc_discovery import ResolvedAuthParameters
    from ps_test_support.mock_oidc_provider import MockOidcProvider

_CLIENT_ID = "ps-cli-integration-test-client"
_CONTEXT_NAME = "test"
_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
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


def _force_no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `build_credential_store()`'s default keyring backend to fall back to file.

    Same mechanism `test_auth_handlers.py::_force_no_keyring` uses -- monkeypatches
    only the three module-level `keyring` functions actually called, never the
    `keyring`/`keyring.errors` symbols themselves (that breaks `KeyringCredentialStore`'s
    own `except keyring.errors.KeyringError` clauses). Portable regardless of the real
    OS keyring state on whatever machine runs this suite.
    """

    def _raise(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr("keyring.get_password", _raise)
    monkeypatch.setattr("keyring.set_password", _raise)
    monkeypatch.setattr("keyring.delete_password", _raise)


def _business_route_transport() -> httpx.MockTransport:
    """A fake PS Service business endpoint (`GET /near-misses`).

    Genuinely enforces the bearer requirement -- 200 with an empty review list
    when a non-empty `Bearer` token is attached, 401 without one -- so a later
    "succeeds with the stored token attached" assertion is meaningful, not
    trivially true (confirmed inline by this test's own unauthenticated-client
    sanity check).
    """

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path != _NEAR_MISSES_PATH:
            return httpx.Response(404)
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer ") or not auth_header.removeprefix("Bearer "):
            return httpx.Response(
                401,
                json={"error": {"code": "unauthenticated", "message": "missing bearer token"}},
            )
        return httpx.Response(200, json={"reviews": []})

    return httpx.MockTransport(_handle)


def test_full_login_call_refresh_logout_cycle_against_generic_mock_oidc_provider(
    mock_oidc_provider: MockOidcProvider,
    fake_ps_service_metadata_server: _FakePsServiceMetadataServer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-019: log in, make an authenticated call, force a refresh, log out.

    One continuous cycle against the real `MockOidcProvider` -- see this
    module's own docstring for why `handle_auth_login` is called directly
    while `config set-context`/`auth logout`/`auth status` go through the
    real `ps_cli.cli.run()`.
    """
    _force_no_keyring(monkeypatch)
    config_dir = resolve_config_dir()

    # --- 1. `config set-context` -- the real handler, via the real CLI entry point;
    # proves the `--auth-audience` flag feeds into a real login next, not just its
    # own isolated test.
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

    # --- 2. `auth login` -- real handle_auth_login, real MockOidcProvider, `sleep`
    # faked to approve the device code on its first invocation (mirrors
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
    credential_store = build_credential_store(config_dir)

    handle_auth_login(
        config.context_name,
        config,
        config_dir=config_dir,
        credential_store=credential_store,
        auth_override=auth_override,
        sleep=_fake_sleep,
    )

    login_output = capsys.readouterr().out
    assert f"logged in to {_CONTEXT_NAME} ({mock_oidc_provider.issuer})" in login_output
    recorded_state = mock_oidc_provider.device_flow_state(captured_device_auth[0].device_code)
    assert recorded_state.audience == "ps-service"  # AC-BI-003's audience passthrough

    # --- 3. A business call, authenticated, against a fake PS Service transport that
    # genuinely enforces the bearer requirement -- confirmed via an unauthenticated
    # client first, so the following "succeeds with the token attached" assertion is
    # meaningful.
    business_transport = _business_route_transport()
    unauthenticated_client = PsServiceClient(
        fake_ps_service_metadata_server.base_url, transport=business_transport
    )
    with pytest.raises(PsCliError) as no_token_excinfo:
        unauthenticated_client.list_pending_reviews()
    assert "authentication rejected" in no_token_excinfo.value.msg

    client = PsServiceClient(
        fake_ps_service_metadata_server.base_url,
        transport=business_transport,
        credential_store=credential_store,
        context=_CONTEXT_NAME,
        auth_override=auth_override,
    )
    result = client.list_pending_reviews()
    assert result.reviews == []

    # --- 4. Force the stored bundle expired; the same call refreshes silently
    # (AC-BI-011/012) and the store afterward holds a *rotated* refresh token.
    stored_before = credential_store.get_tokens(_CONTEXT_NAME)
    assert stored_before is not None
    old_refresh_token = stored_before.refresh_token
    assert old_refresh_token is not None
    credential_store.set_tokens(
        _CONTEXT_NAME,
        TokenBundle(
            access_token=stored_before.access_token,
            refresh_token=old_refresh_token,
            expires_at=0,
            issuer=stored_before.issuer,
        ),
    )

    result_after_refresh = client.list_pending_reviews()
    assert result_after_refresh.reviews == []

    stored_after = credential_store.get_tokens(_CONTEXT_NAME)
    assert stored_after is not None
    assert stored_after.refresh_token != old_refresh_token

    # --- 5. `auth logout` -- the same business call now fails closed (AC-BI-013),
    # and `auth status` reports "not logged in".
    logout_exit_code = run(["auth", "logout", "--context", _CONTEXT_NAME])
    assert logout_exit_code == 0

    with pytest.raises(PsCliError) as logged_out_excinfo:
        client.list_pending_reviews()
    assert "no stored credentials" in logged_out_excinfo.value.msg
    assert "ps-cli auth login" in (logged_out_excinfo.value.hint or "")

    status_exit_code = run(["auth", "status", "--context", _CONTEXT_NAME])
    status_output = capsys.readouterr().out
    assert status_exit_code == 0
    assert f"not logged in to '{_CONTEXT_NAME}'" in status_output
