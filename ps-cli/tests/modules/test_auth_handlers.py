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

Slice 13's happy-path test (and its AC-BI-018 marker-token sibling below) drives
the poll to completion via a `sleep` fake, same convention as `test_device_flow.py`'s
own Slice 10 test -- and since `handle_auth_login` never exposes the freshly-minted
`DeviceAuthorization` to its caller (only `on_device_authorization`'s *internal*
printing sees it), the `device_code` `mock_oidc_provider.complete_device_flow()`
needs is read back from `mock_oidc_provider.last_token_request_form["device_code"]`
-- set at the top of every real `POST /token` this provider handles, so it already
holds the just-polled device_code by the time `sleep` fires. No monkeypatch needed:
`request_device_authorization` is never touched, only observed indirectly through
the provider's own public request-log field.

Issue #163 (Slice O): the one exception is `test_run_auth_login_denied_device_code_
exits_1_with_ac_bi_009_message`, which drives `run()` (real CLI dispatch) rather
than `handle_auth_login` directly -- `run()` exposes no `sleep=` override at all, so
denying the device code strictly before the first real `/token` poll (avoiding a
real `time.sleep`) has no seam other than a monkeypatch spy on `device_flow.
request_device_authorization` that calls straight through to the real function and
reacts to its return value -- see that test's own `# detroit-exception:` comment.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import httpx
import pytest

from ps_cli import device_flow
from ps_cli.cli import run
from ps_cli.config import CliConfig
from ps_cli.credentials import PersistenceCredentialStore, TokenBundle
from ps_cli.errors import CannotVerifyError, CredentialStoreError, PsCliError
from ps_cli.modules.auth_handlers import handle_auth_login, handle_auth_logout, handle_auth_status
from ps_cli.targets import AuthOverrides, ContextEntry, TargetsFile, write_targets
from ps_test_support import mock_oidc_provider as mock_oidc_provider_module
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from conftest import AlwaysRaisingPersistenceBackend, InMemoryPersistenceBackend

    from ps_cli.credentials import CredentialStore
    from ps_cli.device_flow import DeviceAuthorization
    from ps_cli.oidc_discovery import ResolvedAuthParameters
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


# Issue #121, D-121-7: the old "force no real OS credential-storage backend -> fall
# back to file" forcer is gone -- there is no fallback left to force onto. Issue #123:
# tests that construct a `CredentialStore` directly use the shared
# `build_in_memory_persistence` fixture (`conftest.py`) to build a
# `PersistenceCredentialStore`. The three tests below that exercise `run()`'s real
# dispatch chain never reach `build_credential_store()`'s own production wiring before
# failing (their failures all happen during discovery/device-flow, before any
# `credential_store.set_tokens` call), so they also use `build_in_memory_persistence`
# directly for their own post-hoc "nothing was stored" assertion, rather than
# depending on `build_credential_store()`'s real production wiring
# (`credentials.py`'s own module docstring).


# --- Slice 13: handle_auth_login() happy path ---------------------------------------


@pytest.mark.parametrize(
    "oversized_refresh_token",
    [
        pytest.param(None, id="normal_refresh_token"),
        pytest.param("r" * 5000, id="ac_bi_003_oversized_windows_blob_limit_refresh_token"),
    ],
)
def test_handle_auth_login_happy_path_stores_tokens_and_prints_verification_uri(
    mock_oidc_provider: MockOidcProvider,
    fake_json_server_factory: Callable[[str, dict[str, object]], _FakeJsonServer],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
    oversized_refresh_token: str | None,
) -> None:
    """The full Slices 5-12 chain, run once through `handle_auth_login()`.

    Asserts: printed stdout contains the provider's `verification_uri`/`user_code`;
    `credential_store.get_tokens("dev")` returns a `TokenBundle` whose `issuer` matches
    the provider and whose `refresh_token` is the one the poll actually returned --
    issue #121, AC-BI-001: no `access_token` field exists on `TokenBundle` any more.

    Parametrized (issue #123, AC-BI-003) with a 5000-character `refresh_token` --
    `MockOidcProvider._handle_device_code_grant` mints its refresh token via
    `secrets.token_urlsafe(32)` with no seam of its own to override the value, so the
    `oversized_refresh_token`-parametrized case monkeypatches
    `mock_oidc_provider_module.secrets.token_urlsafe` itself, substituting the oversized
    string only for the 32-byte refresh-token call (never the 16-byte device_code call,
    which must keep minting a real unique code for the poll to key off). Proves the full
    CLI-command path --
    discovery, device-authorization, polling, `handle_auth_login`'s own
    `credential_store.set_tokens` call -- round-trips an oversized payload with no
    ceiling, not just `PersistenceCredentialStore` in isolation (that's test 1's job).
    """
    if oversized_refresh_token is not None:
        real_token_urlsafe = mock_oidc_provider_module.secrets.token_urlsafe

        def _fake_token_urlsafe(nbytes: int | None = None) -> str:
            if nbytes == 32:
                return oversized_refresh_token
            return real_token_urlsafe(nbytes)

        monkeypatch.setattr(mock_oidc_provider_module.secrets, "token_urlsafe", _fake_token_urlsafe)

    resource_metadata_server = fake_json_server_factory(
        "/.well-known/oauth-protected-resource", _resource_metadata_body(mock_oidc_provider)
    )
    config = CliConfig(service_url=resource_metadata_server.base_url, context_name="dev")
    credential_store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)

    def fake_sleep(seconds: float) -> None:
        """Approve the device code the just-polled `/token` request used.

        `mock_oidc_provider.last_token_request_form` is set at the top of every real
        `POST /token` this provider handles -- by the time this fires (right after the
        first real "authorization_pending" response), it already holds that poll's own
        `device_code`. No need to intercept `request_device_authorization`'s return
        value at all -- `handle_auth_login`'s own real device-flow call chain is never
        replaced, only `sleep` (its own designed-for-this seam).
        """
        del seconds
        device_code = mock_oidc_provider.last_token_request_form["device_code"]
        mock_oidc_provider.complete_device_flow(device_code)

    handle_auth_login(
        "dev",
        config,
        config_dir=tmp_path,
        credential_store=credential_store,
        auth_override=None,
        sleep=fake_sleep,
    )

    printed = capsys.readouterr().out
    assert f"{mock_oidc_provider.base_url}/device?user_code=" in printed
    assert f"logged in to dev ({mock_oidc_provider.issuer})" in printed

    stored = credential_store.get_tokens("dev")
    assert stored is not None
    assert stored.issuer == mock_oidc_provider.issuer
    assert isinstance(stored.refresh_token, str)
    assert stored.refresh_token != ""
    if oversized_refresh_token is not None:
        assert stored.refresh_token == oversized_refresh_token
        assert len(stored.refresh_token) == 5000


def test_handle_auth_login_no_context_raises_before_any_network_call(
    tmp_path: Path, build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend]
) -> None:
    """`context_name is None` raises up front -- never calls `resolve_auth_parameters`."""
    config = CliConfig(service_url="http://ps-service.invalid", context_name=None)
    credential_store = PersistenceCredentialStore(build_persistence=build_in_memory_persistence)

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
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
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
    assert (
        PersistenceCredentialStore(build_persistence=build_in_memory_persistence).get_tokens("dev")
        is None
    )


def test_run_auth_login_missing_device_authorization_endpoint_exits_1_with_ac_bi_005_message(
    fake_json_server_factory: Callable[[str, dict[str, object]], _FakeJsonServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
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
    assert (
        PersistenceCredentialStore(build_persistence=build_in_memory_persistence).get_tokens("dev")
        is None
    )


def test_run_auth_login_denied_device_code_exits_1_with_ac_bi_009_message(
    mock_oidc_provider: MockOidcProvider,
    fake_json_server_factory: Callable[[str, dict[str, object]], _FakeJsonServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    build_in_memory_persistence: Callable[[str], InMemoryPersistenceBackend],
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

    # `run()`'s real CLI dispatch exposes no `sleep=`/`on_device_authorization=`
    # override at all (unlike `handle_auth_login` called directly elsewhere in this
    # file) -- denial must land strictly before the very first real `/token` poll
    # (`device_auth.interval=1`) or this test would block on a real `time.sleep`
    # waiting for an approval that never comes. This is the only reachable hook
    # between minting and that first poll; the wrapped function still calls straight
    # through to the real `request_device_authorization` unconditionally (a spy, not
    # a stub), so the real device-authorization request always happens for real.
    # detroit-exception: no sleep/on_device_authorization seam reachable via run() -- see above.
    monkeypatch.setattr(
        device_flow, "request_device_authorization", _deny_immediately_after_request
    )

    exit_code = run(["auth", "login"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "denied" in captured.err
    assert (
        PersistenceCredentialStore(build_persistence=build_in_memory_persistence).get_tokens("dev")
        is None
    )


def test_run_auth_login_no_context_exits_1_with_no_context_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No `--context`, no `current_context` set -> exit 1, D-57-5's message; nothing written.

    `build_credential_store()` is constructed by `_dispatch_auth_login` regardless, but
    `handle_auth_login` raises before ever calling any of its methods -- no real OS
    credential-storage backend is ever actually touched, so no portable fake is needed
    here.
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


# --- Issue #179: handle_auth_status() verifies the credential -----------------------
#
# Drives `handle_auth_status` through a fully-synthetic `httpx.MockTransport` standing
# in for PS Service's resource-metadata endpoint and a (loopback) IdP -- the handler's
# own `transport=` seam, mirroring `ensure_valid_access_token`'s.

_STATUS_ISSUER = "http://localhost"
_STATUS_CONFIG = CliConfig(service_url="http://ps-service.example", context_name="dev")
_STATUS_REFRESH_MARKER = "marker-refresh-token-should-never-print-79c3"


def _status_transport(
    handle_token: Callable[[httpx.Request], httpx.Response],
    *,
    resource_metadata_status: int = 200,
) -> httpx.MockTransport:
    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(
                resource_metadata_status,
                json={
                    "resource": "http://ps-service.example",
                    "authorization_servers": [_STATUS_ISSUER],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": _CLIENT_ID,
                },
            )
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": _STATUS_ISSUER,
                    "device_authorization_endpoint": f"{_STATUS_ISSUER}/device_authorization",
                    "token_endpoint": f"{_STATUS_ISSUER}/token",
                },
            )
        return handle_token(request)

    return httpx.MockTransport(_handle)


def _token_ok(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(
        200,
        json={
            "access_token": "at",
            "refresh_token": "rt-rotated",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )


def _status_store(refresh_token: str | None = _STATUS_REFRESH_MARKER) -> _FakeCredentialStore:
    store = _FakeCredentialStore()
    store.set_tokens("dev", TokenBundle(refresh_token=refresh_token, issuer=_STATUS_ISSUER))
    return store


def _run_status(
    store: CredentialStore, transport: httpx.BaseTransport, context: str | None = "dev"
) -> None:
    handle_auth_status(
        context,
        _STATUS_CONFIG,
        credential_store=store,
        auth_override=None,
        transport=transport,
    )


def test_handle_auth_status_with_refreshable_credential_prints_context_issuer_and_logged_in(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-001: a credential that refreshes -> context, issuer, "logged in"; no raise."""
    _run_status(_status_store(), _status_transport(_token_ok))

    printed = capsys.readouterr().out
    assert "context: dev" in printed
    assert f"issuer: {_STATUS_ISSUER}" in printed
    assert "logged in" in printed
    assert "not usable" not in printed


def test_handle_auth_status_persists_the_rotated_refresh_token() -> None:
    """AC-BI-006: the rotated refresh token is stored, so the next run still works."""
    store = _status_store()

    _run_status(store, _status_transport(_token_ok))

    stored = store.get_tokens("dev")
    assert stored is not None
    assert stored.refresh_token == "rt-rotated"


def test_handle_auth_status_with_rejected_credential_reports_not_usable_and_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-002: an `invalid_grant` -> "not usable", log-in hint, non-zero, never "logged in"."""

    def _invalid_grant(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(PsCliError) as excinfo:
        _run_status(_status_store(), _status_transport(_invalid_grant))

    assert not isinstance(excinfo.value, CannotVerifyError)
    assert "not usable" in excinfo.value.msg
    assert "ps-cli auth login" in (excinfo.value.hint or "")
    assert "logged in" not in capsys.readouterr().out


def test_handle_auth_status_with_no_refresh_token_reports_not_usable() -> None:
    """AC-BI-002: a stored bundle with no refresh token cannot ever refresh."""

    def _unreachable(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request.url)

    with pytest.raises(PsCliError) as excinfo:
        _run_status(_status_store(refresh_token=None), _status_transport(_unreachable))

    assert "not usable" in excinfo.value.msg


def test_handle_auth_status_with_no_stored_credential_prints_not_logged_in_and_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-BI-003: nothing stored -> "not logged in" and a non-zero exit (reverses D-121-6)."""
    with pytest.raises(PsCliError) as excinfo:
        _run_status(_FakeCredentialStore(), _status_transport(_token_ok))

    assert "not logged in to 'dev'" in excinfo.value.msg
    assert "ps-cli auth login" in (excinfo.value.hint or "")
    assert "logged in\n" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "scenario",
    ["idp_connect_error", "idp_timeout", "idp_5xx", "service_connect_error", "service_5xx"],
)
def test_handle_auth_status_when_unverifiable_reports_cannot_verify_not_logged_in_or_unusable(
    scenario: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-004: transport failures and 5xx say nothing about the credential."""

    def _handle_token(request: httpx.Request) -> httpx.Response:
        if scenario == "idp_connect_error":
            raise httpx.ConnectError("refused", request=request)
        if scenario == "idp_timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(503)

    def _handle(request: httpx.Request) -> httpx.Response:
        if scenario.startswith("service") and request.url.path.endswith("protected-resource"):
            if scenario == "service_connect_error":
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(503)
        return _status_transport(_handle_token).handle_request(request)

    with pytest.raises(PsCliError) as excinfo:
        _run_status(_status_store(), httpx.MockTransport(_handle))

    assert "cannot verify" in excinfo.value.msg
    assert "not usable" not in excinfo.value.msg
    assert "not logged in" not in excinfo.value.msg
    assert "ps-cli auth login" not in (excinfo.value.hint or "")
    assert "logged in\n" not in capsys.readouterr().out


def test_handle_auth_status_when_credential_store_is_inaccessible_reports_store_hint(
    build_always_raising_persistence: Callable[[str], AlwaysRaisingPersistenceBackend],
) -> None:
    """AC-BI-010: a store that raises on read is neither "not logged in" nor "not usable"."""
    store = PersistenceCredentialStore(build_persistence=build_always_raising_persistence)

    with pytest.raises(PsCliError) as excinfo:
        _run_status(store, _status_transport(_token_ok))

    assert "credential store" in excinfo.value.msg
    assert "available and unlocked" in (excinfo.value.hint or "")
    assert "not usable" not in excinfo.value.msg
    assert "not logged in" not in excinfo.value.msg


class _WriteFailingCredentialStore(_FakeCredentialStore):
    """Reads work; persisting the rotated token raises, as a locked keychain would."""

    def set_tokens(self, context: str, tokens: TokenBundle) -> None:
        del context, tokens
        raise CredentialStoreError(
            msg="could not access the credential store for context 'dev'",
            hint="the credential-storage backend raised OSError; check that it is available "
            "and unlocked",
        )


def test_handle_auth_status_when_persisting_the_rotated_token_fails_reports_store_error() -> None:
    """AC-BI-010: a store failure on the post-refresh write is not "not usable"."""
    store = _WriteFailingCredentialStore()
    _FakeCredentialStore.set_tokens(
        store, "dev", TokenBundle(refresh_token="rt", issuer=_STATUS_ISSUER)
    )

    with pytest.raises(CredentialStoreError) as excinfo:
        _run_status(store, _status_transport(_token_ok))

    assert "credential store" in excinfo.value.msg
    assert "available and unlocked" in (excinfo.value.hint or "")
    assert "not usable" not in excinfo.value.msg


def test_handle_auth_status_with_no_context_raises_ps_cli_error() -> None:
    """AC-BI-009: `context_name is None` raises up front (D-57-5), as `handle_auth_login` does."""
    with pytest.raises(PsCliError) as excinfo:
        _run_status(_FakeCredentialStore(), _status_transport(_token_ok), context=None)

    assert "no context to authenticate" in excinfo.value.msg
    assert "ps-cli config set-context" in excinfo.value.msg


@pytest.mark.parametrize("outcome", ["logged_in", "rejected", "unverifiable"])
def test_handle_auth_status_output_never_contains_a_secret(
    outcome: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-005: neither the stored refresh token nor the access token ever prints."""

    def _handle_token(request: httpx.Request) -> httpx.Response:
        if outcome == "logged_in":
            return _token_ok(request)
        if outcome == "rejected":
            return httpx.Response(400, json={"error": "invalid_grant"})
        raise httpx.ConnectError("refused", request=request)

    try:
        _run_status(_status_store(), _status_transport(_handle_token))
    except PsCliError as error:
        raised_text = str(error)
    else:
        raised_text = ""

    printed = capsys.readouterr()
    for secret in (_STATUS_REFRESH_MARKER, "rt-rotated", "at"):
        assert secret not in printed.out.split()
        assert secret not in printed.err.split()
        assert secret not in raised_text.split()


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


def test_handle_auth_login_output_never_contains_the_stored_refresh_token_value(
    mock_oidc_provider: MockOidcProvider,
    fake_json_server_factory: Callable[[str, dict[str, object]], _FakeJsonServer],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auth login`'s confirmation line and verification-URI printout never contain
    the stored refresh-token value (AC-BI-018), proven with a distinctive marker
    value forced through the real device-flow/token-minting path (same
    `mock_oidc_provider_module.secrets.token_urlsafe` approved-boundary seam the
    oversized-refresh-token happy-path test above uses) -- not a full stub of
    `resolve_auth_parameters`/`complete_device_login` that never actually exercises
    real device-flow/refresh-token-minting code at all.

    No `access_token` marker check any more: `_print_device_authorization` only ever
    prints `verification_uri`/`user_code`, and `handle_auth_login`'s own confirmation
    line only ever prints `issuer` -- neither ever touches an access token, and
    (issue #121, AC-BI-001) `TokenBundle` has no `access_token` field to persist
    either, so that half of AC-BI-018 is a structural guarantee, not something this
    test needs to separately prove.
    """
    marker_refresh_token = "marker-refresh-token-should-never-print-79c3"
    real_token_urlsafe = mock_oidc_provider_module.secrets.token_urlsafe

    def _fake_token_urlsafe(nbytes: int | None = None) -> str:
        if nbytes == 32:
            return marker_refresh_token
        return real_token_urlsafe(nbytes)

    monkeypatch.setattr(mock_oidc_provider_module.secrets, "token_urlsafe", _fake_token_urlsafe)

    resource_metadata_server = fake_json_server_factory(
        "/.well-known/oauth-protected-resource", _resource_metadata_body(mock_oidc_provider)
    )
    config = CliConfig(service_url=resource_metadata_server.base_url, context_name="dev")
    credential_store = _FakeCredentialStore()

    def fake_sleep(seconds: float) -> None:
        """Approve the device code the just-polled `/token` request used (see the
        happy-path test above for why `last_token_request_form` needs no spy).
        """
        del seconds
        device_code = mock_oidc_provider.last_token_request_form["device_code"]
        mock_oidc_provider.complete_device_flow(device_code)

    handle_auth_login(
        "dev",
        config,
        config_dir=tmp_path,
        credential_store=credential_store,
        auth_override=None,
        sleep=fake_sleep,
    )

    printed = capsys.readouterr()
    assert marker_refresh_token not in printed.out
    assert marker_refresh_token not in printed.err
    # Sanity: the marker refresh_token really was stored -- this test exercised the
    # real value, not a stand-in that the code path never actually touched.
    stored = credential_store.get_tokens("dev")
    assert stored is not None
    assert stored.refresh_token == marker_refresh_token
