"""Tests for ps_cli.modules.auth_handlers (issue #57 Slices 13-14, 19-20).

`handle_auth_login` has no `transport` seam of its own -- unlike
`oidc_discovery.resolve_auth_parameters`, which `test_oidc_discovery.py`/
`test_device_flow.py` inject an `httpx.MockTransport` into directly, this
module's dispatch adapter calls `resolve_auth_parameters(config.service_url,
auth_override)` with no transport override at all. So PS Service's own
resource-metadata endpoint (and, for Slice 14's AC-BI-005 case, a "no device
flow" issuer) must be a real, local, loopback HTTP server for these tests --
exactly the same problem `ps_test_support.mock_oidc_provider.MockOidcProvider`
solves on the IdP side, for the same reason (see that module's own docstring).
`_FakeJsonServer` below plays that role, generalized to serve any one JSON
body at any one path.

Slice 13's happy-path test drives the poll to completion via a `sleep` fake,
same convention as `test_device_flow.py`'s own Slice 10 test -- but since
`handle_auth_login` never exposes the freshly-minted `DeviceAuthorization` to
its caller (only `on_device_authorization`'s *internal* printing sees it),
the `device_code` `mock_oidc_provider.complete_device_flow()` needs is
captured via a `monkeypatch`-installed spy around `device_flow.
request_device_authorization` that calls straight through to the real
function and simply records what it returned -- never replacing real
provider behavior, only observing it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import pytest

from ps_cli import device_flow, oidc_discovery
from ps_cli.cli import run
from ps_cli.config import CliConfig
from ps_cli.credentials import KeyringCredentialStore, TokenBundle
from ps_cli.device_flow import DeviceAuthorization, TokenResponse
from ps_cli.errors import PsCliError
from ps_cli.modules.auth_handlers import handle_auth_login, handle_auth_logout, handle_auth_status
from ps_cli.oidc_discovery import ResolvedAuthParameters
from ps_cli.targets import AuthOverrides, ContextEntry, TargetsFile, write_targets
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import httpx
    from conftest import InMemoryKeyringBackend

    from ps_test_support.mock_oidc_provider import MockOidcProvider

_CLIENT_ID = "ps-cli-test-client"


def _build_fake_json_handler(path: str, body: dict[str, object]) -> type[BaseHTTPRequestHandler]:
    """Build a `BaseHTTPRequestHandler` subclass serving `body` at exactly `path`.

    Closure-based factory, same recipe as `mock_oidc_provider.py::_build_handler_class`
    -- `HTTPServer` requires a handler *class*, not an instance, so `path`/`body` must be
    captured some way other than `self`.
    """

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Silence the default stderr access log."""

        def do_GET(self) -> None:
            if self.path == path:
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


class _FakeJsonServer:
    """A real, ephemeral-port local HTTP server serving one fixed JSON body at one path.

    Stands in for PS Service's own resource-metadata endpoint (and, for one Slice 14
    case, a "no device flow" issuer's discovery document) -- see this module's own
    docstring for why a real loopback server, not an `httpx.MockTransport`, is the
    seam available here.
    """

    def __init__(self, path: str, body: dict[str, object]) -> None:
        self._server = HTTPServer(("127.0.0.1", 0), _build_fake_json_handler(path, body))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}"

    def shutdown(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def fake_json_server_factory() -> Iterator[Callable[[str, dict[str, object]], _FakeJsonServer]]:
    """A factory building `_FakeJsonServer`s for this test, shut down afterward."""
    servers: list[_FakeJsonServer] = []

    def _make(path: str, body: dict[str, object]) -> _FakeJsonServer:
        server = _FakeJsonServer(path, body)
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.shutdown()


def _resource_metadata_body(
    provider: MockOidcProvider, *, client_id: str | None = _CLIENT_ID
) -> dict[str, object]:
    """The resource-metadata JSON body PS Service would serve, pointing at `provider`."""
    body: dict[str, object] = {
        "resource": "http://ps-service.example",
        "authorization_servers": [provider.issuer],
        "scopes_supported": ["openid"],
    }
    if client_id is not None:
        body["ps_cli_client_id"] = client_id
    return body


# Issue #121, D-121-7: `_force_no_keyring` (a "no real OS keyring -> fall back to file"
# forcer) is gone -- there is no fallback left to force onto. Tests below that need
# `build_credential_store()`'s real, zero-argument production wiring to actually work
# portably use the shared `portable_keyring` fixture (`conftest.py`) instead; tests that
# construct a `CredentialStore` directly use the shared `keyring_backend` fixture.


# --- Slice 13: handle_auth_login() happy path ---------------------------------------


def test_handle_auth_login_happy_path_stores_tokens_and_prints_verification_uri(
    mock_oidc_provider: MockOidcProvider,
    fake_json_server_factory: Callable[[str, dict[str, object]], _FakeJsonServer],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """The full Slices 5-12 chain, run once through `handle_auth_login()`.

    Asserts: printed stdout contains the provider's `verification_uri`/`user_code`;
    `credential_store.get_tokens("dev")` returns a `TokenBundle` whose `issuer` matches
    the provider and whose `refresh_token` is the one the poll actually returned --
    issue #121, AC-BI-001: no `access_token` field exists on `TokenBundle` any more.
    """
    resource_metadata_server = fake_json_server_factory(
        "/.well-known/oauth-protected-resource", _resource_metadata_body(mock_oidc_provider)
    )
    config = CliConfig(service_url=resource_metadata_server.base_url, context_name="dev")
    credential_store = KeyringCredentialStore(keyring_backend=keyring_backend)

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

    def fake_sleep(seconds: float) -> None:
        del seconds
        mock_oidc_provider.complete_device_flow(captured_device_auth[0].device_code)

    handle_auth_login(
        "dev",
        config,
        config_dir=tmp_path,
        credential_store=credential_store,
        auth_override=None,
        sleep=fake_sleep,
    )

    printed = capsys.readouterr().out
    device_auth = captured_device_auth[0]
    assert device_auth.user_code in printed
    assert device_auth.verification_uri in printed
    assert f"logged in to dev ({mock_oidc_provider.issuer})" in printed

    stored = credential_store.get_tokens("dev")
    assert stored is not None
    assert stored.issuer == mock_oidc_provider.issuer
    assert isinstance(stored.refresh_token, str)
    assert stored.refresh_token != ""


def test_handle_auth_login_no_context_raises_before_any_network_call(
    tmp_path: Path, keyring_backend: InMemoryKeyringBackend
) -> None:
    """`context_name is None` raises up front -- never calls `resolve_auth_parameters`."""
    config = CliConfig(service_url="http://ps-service.invalid", context_name=None)
    credential_store = KeyringCredentialStore(keyring_backend=keyring_backend)

    with pytest.raises(PsCliError) as excinfo:
        handle_auth_login(
            None,
            config,
            config_dir=tmp_path,
            credential_store=credential_store,
            auth_override=None,
        )

    assert "no context to authenticate" in excinfo.value.msg
    assert "ps-cli config set-context" in excinfo.value.msg


# --- Slice 14: auth login failure branches, via run() --------------------------------


def _seed_dev_context(config_dir: Path, *, url: str, auth: AuthOverrides | None = None) -> None:
    """Write `targets.toml` with a single `dev` context, selected as `current_context`."""
    write_targets(
        config_dir,
        TargetsFile(current_context="dev", contexts={"dev": ContextEntry(url=url, auth=auth)}),
    )


def test_run_auth_login_missing_client_id_exits_1_with_ac_bi_004_message(
    mock_oidc_provider: MockOidcProvider,
    fake_json_server_factory: Callable[[str, dict[str, object]], _FakeJsonServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """No `ps_cli_client_id` in metadata, no override -> exit 1, AC-BI-004's message."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(config_dir))
    resource_metadata_server = fake_json_server_factory(
        "/.well-known/oauth-protected-resource",
        _resource_metadata_body(mock_oidc_provider, client_id=None),
    )
    _seed_dev_context(config_dir, url=resource_metadata_server.base_url)

    exit_code = run(["auth", "login"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No OIDC client id is configured" in captured.err
    assert KeyringCredentialStore(keyring_backend=portable_keyring).get_tokens("dev") is None


def test_run_auth_login_missing_device_authorization_endpoint_exits_1_with_ac_bi_005_message(
    fake_json_server_factory: Callable[[str, dict[str, object]], _FakeJsonServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """An issuer whose discovery doc has no `device_authorization_endpoint` -> exit 1,
    AC-BI-005's message naming that issuer.
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(config_dir))
    # No `device_authorization_endpoint` (nor `issuer`, which `resolve_auth_parameters`
    # never actually reads off this response -- it always uses the resolved issuer URL
    # itself, see `oidc_discovery.py::resolve_auth_parameters`'s own docstring). A bare
    # `{"issuer": ...}` body is enough to satisfy `_parse_openid_discovery_document`'s
    # only required field.
    issuer_server = fake_json_server_factory(
        "/.well-known/openid-configuration", {"issuer": "http://placeholder.invalid"}
    )
    resource_metadata_server = fake_json_server_factory(
        "/.well-known/oauth-protected-resource",
        {
            "resource": "http://ps-service.example",
            "authorization_servers": [issuer_server.base_url],
            "scopes_supported": ["openid"],
            "ps_cli_client_id": _CLIENT_ID,
        },
    )
    _seed_dev_context(config_dir, url=resource_metadata_server.base_url)

    exit_code = run(["auth", "login"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert issuer_server.base_url in captured.err
    assert "does not support device authorization" in captured.err
    assert KeyringCredentialStore(keyring_backend=portable_keyring).get_tokens("dev") is None


def test_run_auth_login_denied_device_code_exits_1_with_ac_bi_009_message(
    mock_oidc_provider: MockOidcProvider,
    fake_json_server_factory: Callable[[str, dict[str, object]], _FakeJsonServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    portable_keyring: InMemoryKeyringBackend,
) -> None:
    """`deny_device_code` fired the instant the device code is minted (before the poll
    even starts) -> the first `/token` poll returns `access_denied` -> exit 1, AC-BI-009's
    message.
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(config_dir))
    resource_metadata_server = fake_json_server_factory(
        "/.well-known/oauth-protected-resource", _resource_metadata_body(mock_oidc_provider)
    )
    _seed_dev_context(config_dir, url=resource_metadata_server.base_url)

    original_request_device_authorization = device_flow.request_device_authorization

    def _deny_immediately_after_request(
        params: ResolvedAuthParameters, *, transport: httpx.BaseTransport | None = None
    ) -> DeviceAuthorization:
        result = original_request_device_authorization(params, transport=transport)
        mock_oidc_provider.deny_device_code(result.device_code)
        return result

    monkeypatch.setattr(
        device_flow, "request_device_authorization", _deny_immediately_after_request
    )

    exit_code = run(["auth", "login"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "denied" in captured.err
    assert KeyringCredentialStore(keyring_backend=portable_keyring).get_tokens("dev") is None


def test_run_auth_login_no_context_exits_1_with_no_context_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No `--context`, no `current_context` set -> exit 1, D-57-5's message; nothing written.

    `build_credential_store()` is constructed by `_dispatch_auth_login` regardless, but
    `handle_auth_login` raises before ever calling any of its methods -- no real OS
    keyring backend is ever actually touched, so no portable fake is needed here.
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(config_dir))

    exit_code = run(["auth", "login"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no context to authenticate" in captured.err
    assert "ps-cli config set-context" in captured.err


# --- Slices 19-20: handle_auth_status()/handle_auth_logout() ------------------------


class _FakeCredentialStore:
    """A minimal dict-backed `CredentialStore` double for these tests.

    Same shape as `test_device_flow.py`'s own `_FakeCredentialStore` -- used here
    rather than `FileCredentialStore` so these tests observe *only* what
    `handle_auth_status`/`handle_auth_logout` themselves print, not
    `FileCredentialStore`'s own unconditional stderr fallback warning
    (`credentials.py::FileCredentialStore._warn_fallback`), which would otherwise
    contaminate the "prints nothing on success"/exact-output assertions below.
    """

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


def test_handle_auth_status_with_stored_tokens_prints_context_issuer_and_logged_in(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pre-seeded bundle -> `context`/`issuer`/`logged in`, correctly rendered
    (issue #121, D-121-6) -- no `subject`/`expiry` any more, since `TokenBundle` no
    longer carries an `access_token`/`expires_at` to derive either from.
    """
    credential_store = _FakeCredentialStore()
    credential_store.set_tokens(
        "dev", TokenBundle(refresh_token="rt", issuer="https://issuer.example")
    )

    handle_auth_status("dev", credential_store=credential_store)

    printed = capsys.readouterr().out
    assert "context: dev" in printed
    assert "issuer: https://issuer.example" in printed
    assert "logged in" in printed
    assert "subject" not in printed
    assert "expiry" not in printed


def test_handle_auth_status_with_no_stored_tokens_prints_not_logged_in_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No stored bundle -> a plain status line, no exception (exit code 0)."""
    credential_store = _FakeCredentialStore()

    handle_auth_status("dev", credential_store=credential_store)

    printed = capsys.readouterr().out
    assert printed == "not logged in to 'dev'\n"


def test_handle_auth_status_with_no_context_raises_ps_cli_error() -> None:
    """`context_name is None` raises up front (D-57-5), same as `handle_auth_login`."""
    credential_store = _FakeCredentialStore()

    with pytest.raises(PsCliError) as excinfo:
        handle_auth_status(None, credential_store=credential_store)

    assert "no context to authenticate" in excinfo.value.msg
    assert "ps-cli config set-context" in excinfo.value.msg


def test_handle_auth_logout_deletes_stored_tokens() -> None:
    """A pre-seeded bundle -> gone afterward (AC-BI-016's real removal)."""
    credential_store = _FakeCredentialStore()
    credential_store.set_tokens(
        "dev", TokenBundle(refresh_token="rt", issuer="https://issuer.example")
    )

    handle_auth_logout("dev", credential_store=credential_store)

    assert credential_store.get_tokens("dev") is None


def test_handle_auth_logout_on_a_context_with_no_stored_tokens_is_a_noop() -> None:
    """No stored bundle -> no exception, still nothing stored afterward."""
    credential_store = _FakeCredentialStore()

    handle_auth_logout("dev", credential_store=credential_store)

    assert credential_store.get_tokens("dev") is None


def test_handle_auth_logout_prints_nothing_on_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Silence on success -- unlike `auth login`'s own confirmation line."""
    credential_store = _FakeCredentialStore()
    credential_store.set_tokens(
        "dev", TokenBundle(refresh_token="rt", issuer="https://issuer.example")
    )

    handle_auth_logout("dev", credential_store=credential_store)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# --- Slice 22: AC-BI-018 never log a token value -------------------------------------


def test_handle_auth_login_output_never_contains_the_access_or_refresh_token_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auth login`'s confirmation line and verification-URI printout never contain
    the raw access/refresh token value (AC-BI-018).

    `resolve_auth_parameters`/`complete_device_login` are monkeypatched to known
    marker values -- proving the property structurally, for whatever an IdP might
    ever mint, not just for what today's mock provider happens to generate.
    """
    marker_access_token = "marker-access-token-should-never-print-79c3"
    marker_refresh_token = "marker-refresh-token-should-never-print-79c3"
    params = ResolvedAuthParameters(
        issuer="http://127.0.0.1:1",
        client_id="cli-client-id",
        scopes=("openid",),
        audience=None,
        device_authorization_endpoint="http://127.0.0.1:1/device_authorization",
        token_endpoint="http://127.0.0.1:1/token",
    )
    device_auth = DeviceAuthorization(
        device_code="dc",
        user_code="uc-marker-user-code",
        verification_uri="http://127.0.0.1:1/device",
        verification_uri_complete=None,
        expires_in=600,
        interval=1,
    )

    def _fake_resolve(
        service_url: str, override: object, *, transport: object = None
    ) -> ResolvedAuthParameters:
        del service_url, override, transport
        return params

    def _fake_complete_device_login(
        params_: ResolvedAuthParameters,
        *,
        transport: object = None,
        sleep: object = None,
        on_device_authorization: Callable[[DeviceAuthorization], None] | None = None,
    ) -> TokenResponse:
        del params_, transport, sleep
        if on_device_authorization is not None:
            on_device_authorization(device_auth)
        return TokenResponse(
            access_token=marker_access_token,
            refresh_token=marker_refresh_token,
            expires_in=3600,
        )

    monkeypatch.setattr(oidc_discovery, "resolve_auth_parameters", _fake_resolve)
    monkeypatch.setattr(device_flow, "complete_device_login", _fake_complete_device_login)

    config = CliConfig(service_url="http://ps-service.example", context_name="dev")
    credential_store = _FakeCredentialStore()

    handle_auth_login(
        "dev",
        config,
        config_dir=tmp_path,
        credential_store=credential_store,
        auth_override=None,
    )

    printed = capsys.readouterr()
    assert marker_access_token not in printed.out
    assert marker_access_token not in printed.err
    assert marker_refresh_token not in printed.out
    assert marker_refresh_token not in printed.err
    # Sanity: the marker refresh_token really was stored -- this test exercised the
    # real value, not a stand-in that the code path never actually touched. There is
    # no `stored.access_token` to check any more (issue #121, AC-BI-001) -- that it
    # was never persisted is now a structural guarantee (`TokenBundle` has no such
    # field), not something this test needs to separately prove.
    stored = credential_store.get_tokens("dev")
    assert stored is not None
    assert stored.refresh_token == marker_refresh_token


def test_handle_auth_status_output_never_contains_the_stored_refresh_token_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`auth status`'s report prints `context`/`issuer`/`logged in` (issue #121,
    D-121-6) but never the stored `refresh_token` value itself (AC-BI-018) -- the
    only secret-shaped field left on `TokenBundle` for this command to ever touch.
    """
    refresh_marker = "marker-refresh-token-should-never-print-79c3"
    credential_store = _FakeCredentialStore()
    credential_store.set_tokens(
        "dev", TokenBundle(refresh_token=refresh_marker, issuer="https://issuer.example")
    )

    handle_auth_status("dev", credential_store=credential_store)

    printed = capsys.readouterr()
    assert "context: dev" in printed.out
    assert "issuer: https://issuer.example" in printed.out
    assert refresh_marker not in printed.out
    assert refresh_marker not in printed.err
