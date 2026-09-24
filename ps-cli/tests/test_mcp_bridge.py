"""Tests for ps_cli.mcp_bridge (issue #118): structured lifecycle/health logging.

`_BridgeContext`, `_forward_message`, `_log`, `_open_log_file` are private helpers,
unit-tested directly per this repo's existing precedent (`test_config.py`'s
`_deep_merge`) -- imported with `# pyright: ignore[reportPrivateUsage]`. `main()` is
the module's one real public entry point (the `ps-cli-mcp-bridge` console script) and
is exercised end-to-end via a real `httpx.MockTransport` and a stubbed `sys.stdin`,
mirroring `device_flow.py`'s own `transport` constructor-injection seam.
"""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from ps_cli.credentials import KeyringCredentialStore, TokenBundle
from ps_cli.device_flow import AccessTokenCache
from ps_cli.mcp_bridge import (
    _LOG_FILE_NAME,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _BridgeContext,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _forward_message,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _log,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _open_log_file,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    main,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

    from conftest import InMemoryKeyringBackend

_MCP_URL = "https://ps.example.test/mcp/"
_SERVICE_URL = "https://ps.example.test"

# Issue #121 Slice 2's own fake issuer/discovery constants -- mirrors
# `test_http_client.py`'s `_build_auth_and_business_transport` group (CHANGES.md
# MINOR-2's "fake-transport, refresh-call-counting" design), applied at the
# mcp_bridge entrypoint via `_BridgeContext.transport` (the seam `_resolve_access_token`
# threads into `ensure_valid_access_token`, added in this slice -- see mcp_bridge.py).
_FAKE_ISSUER = "https://issuer.example"
_FAKE_CLIENT_ID = "ps-cli-test-client"
_RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
_OPENID_CONFIGURATION_URL = f"{_FAKE_ISSUER}/.well-known/openid-configuration"
_TOKEN_URL = f"{_FAKE_ISSUER}/token"


def _in_memory_credential_store(keyring_backend: InMemoryKeyringBackend) -> KeyringCredentialStore:
    """A fresh, portable `KeyringCredentialStore` backed by an in-memory fake (D-121-7).

    Replaces `FileCredentialStore(tmp_path)` (issue #121: that class is gone entirely,
    AC-BI-008) -- just enough to keep this suite green; Slice 2 adds the
    bridge-specific reuse-across-messages/expiry-recovery proofs on top. Takes the
    shared `keyring_backend` fixture (`conftest.py`) as a parameter rather than
    constructing one itself -- pytest's `--import-mode=importlib` (this repo's
    convention, `pyproject.toml`) means `conftest.py`'s classes can only be
    instantiated via fixture injection, never a direct `from conftest import ...` at
    module level in a test file.
    """
    return KeyringCredentialStore(keyring_backend=keyring_backend)


def _ctx(
    tmp_path: Path,
    keyring_backend: InMemoryKeyringBackend,
    *,
    context_name: str | None = None,
    log_file: TextIO | None = None,
    access_token_cache: AccessTokenCache | None = None,
) -> _BridgeContext:
    """Build a `_BridgeContext` with literal values -- no real config/service needed."""
    del tmp_path  # unused now that credential storage is keyring-only, not file-based
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=_in_memory_credential_store(keyring_backend),
        access_token_cache=(
            access_token_cache if access_token_cache is not None else AccessTokenCache()
        ),
        log_file=log_file,
    )


def _ctx_with_stale_bundle(
    keyring_backend: InMemoryKeyringBackend, *, context_name: str, log_file: TextIO | None = None
) -> _BridgeContext:
    """A `_BridgeContext` whose stored bundle has no `refresh_token` -- the very next
    `ensure_valid_access_token` call fails closed (AC-BI-002), with an empty (never
    populated) `AccessTokenCache`.
    """
    store = _in_memory_credential_store(keyring_backend)
    store.set_tokens(context_name, TokenBundle(refresh_token=None, issuer="https://idp.example"))
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=store,
        access_token_cache=AccessTokenCache(),
        log_file=log_file,
    )


def _ctx_with_cached_access_token(
    keyring_backend: InMemoryKeyringBackend,
    *,
    context_name: str,
    access_token: str,
    log_file: TextIO | None = None,
) -> _BridgeContext:
    """A `_BridgeContext` whose `access_token_cache` is pre-populated with `access_token`.

    `ensure_valid_access_token`'s cache-hit branch then returns `access_token`
    immediately, with no refresh network call -- `mcp_bridge.py` has no `transport`
    seam into the refresh path (see its own module docstring), so a real refresh
    cannot be simulated here without adding one; pre-populating the cache is the
    direct, minimal way to exercise `_forward_message`'s own bearer-attachment
    behavior in isolation (issue #121, D-121-2). A stored bundle with a non-`None`
    `refresh_token` is also needed so `_resolve_access_token`'s own "nothing stored
    for this context" check doesn't short-circuit before ever consulting the cache.
    """
    store = _in_memory_credential_store(keyring_backend)
    store.set_tokens(
        context_name, TokenBundle(refresh_token="rt-unused", issuer="https://idp.example")
    )
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=store,
        access_token_cache=AccessTokenCache(token=access_token, expires_at=99999999999),
        log_file=log_file,
    )


def _build_auth_and_mcp_transport(
    mcp_handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.MockTransport, list[int]]:
    """One fake transport answering resource-metadata/discovery/refresh + the forwarded
    MCP business call (issue #121 Slice 2) -- the mcp_bridge-entrypoint analog of
    `test_http_client.py`'s own `_build_auth_and_business_transport`, per CHANGES.md
    MINOR-2's "fake-transport, refresh-call-counting" design (a genuine wire-level
    fake, not a `resolve_auth_parameters`/`_refresh_tokens` monkeypatch).

    Returns `(transport, call_count)` where `call_count[0]` is mutated on every
    `POST <issuer>/token` refresh call, so a test can assert exactly how many
    refreshes happened across any number of `_forward_message` calls sharing it.
    """
    call_count = [0]

    def _handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == _RESOURCE_METADATA_PATH:
            return httpx.Response(
                200,
                json={
                    "resource": _SERVICE_URL,
                    "authorization_servers": [_FAKE_ISSUER],
                    "scopes_supported": ["openid"],
                    "ps_cli_client_id": _FAKE_CLIENT_ID,
                },
            )
        if str(request.url) == _OPENID_CONFIGURATION_URL:
            return httpx.Response(
                200,
                json={
                    "issuer": _FAKE_ISSUER,
                    "device_authorization_endpoint": f"{_FAKE_ISSUER}/device_authorization",
                    "token_endpoint": _TOKEN_URL,
                },
            )
        if str(request.url) == _TOKEN_URL:
            call_count[0] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"refreshed-token-{call_count[0]}",
                    "refresh_token": "rt-rotated",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        return mcp_handler(request)

    return httpx.MockTransport(_handle), call_count


def _ctx_with_refresh_token_and_transport(
    keyring_backend: InMemoryKeyringBackend,
    *,
    context_name: str,
    transport: httpx.BaseTransport,
) -> _BridgeContext:
    """A `_BridgeContext` with a stored `refresh_token` and no cached access token yet,
    wired to `transport` so `ensure_valid_access_token`'s real refresh path (not a
    pre-populated cache) is what `_forward_message` actually exercises (issue #121
    Slice 2) -- distinct from `_ctx_with_cached_access_token` above, which exists
    specifically to bypass the refresh path for tests that don't care about it.
    """
    store = _in_memory_credential_store(keyring_backend)
    store.set_tokens(context_name, TokenBundle(refresh_token="seed-rt", issuer=_FAKE_ISSUER))
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url=_SERVICE_URL,
        auth_override=None,
        credential_store=store,
        access_token_cache=AccessTokenCache(),
        log_file=None,
        transport=transport,
    )


# --- Group 5: Issue #121 Slice 2 -- one refresh reused across many forwarded messages ---


class TestOneRefreshReusedAcrossManyForwardedMessages:
    """AC-BI-003/004 at the mcp_bridge entrypoint's own scale: many forwarded JSON-RPC
    messages per long-lived proxy-loop invocation, distinct from Slice 1's proof
    (many `PsServiceClient` business calls per instance, a different object with a
    different call pattern).
    """

    def test_two_forwarded_messages_share_exactly_one_refresh_and_identical_bearer_header(
        self, keyring_backend: InMemoryKeyringBackend
    ) -> None:
        """PLAN.md Slice 2's original ask: two forwarded messages against a fake PS
        Service MCP endpoint requiring a bearer token trigger exactly one refresh,
        and both messages' `Authorization` headers are identical.
        """
        captured_headers: list[str | None] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        transport, refresh_calls = _build_auth_and_mcp_transport(_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            keyring_backend, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)

        _forward_message(
            client,
            {"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            session_id=None,
            ctx=ctx,
        )
        _forward_message(
            client,
            {"jsonrpc": "2.0", "method": "tools/call", "id": 2},
            session_id=None,
            ctx=ctx,
        )

        assert refresh_calls == [1]
        assert captured_headers == ["Bearer refreshed-token-1", "Bearer refreshed-token-1"]

    def test_stale_cached_token_between_two_forwarded_messages_triggers_a_second_refresh(
        self, keyring_backend: InMemoryKeyringBackend
    ) -> None:
        """CHANGES.md CRITICAL-1's own regression proof: a long-lived mcp_bridge
        process whose cached token goes stale mid-session (wall-clock advances past
        `expires_at` between two forwarded messages) self-heals on the very next
        forwarded message -- the refresh-endpoint call count becomes 2, and the
        second message's `Authorization` header carries the NEW token, not the
        first (stale) one reused forever.

        Simulates the wall-clock advance by mutating the shared `AccessTokenCache`'s
        `expires_at` directly to an already-past epoch between the two
        `_forward_message` calls -- `_is_cache_stale` reads `cache.expires_at`
        fresh on every call, so this is equivalent to, and simpler than,
        monkeypatching `time.time()` in `device_flow.py`.
        """
        captured_headers: list[str | None] = []

        def _mcp_handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

        transport, refresh_calls = _build_auth_and_mcp_transport(_mcp_handler)
        ctx = _ctx_with_refresh_token_and_transport(
            keyring_backend, context_name="prod", transport=transport
        )
        client = httpx.Client(transport=transport)

        _forward_message(
            client,
            {"jsonrpc": "2.0", "method": "tools/call", "id": 1},
            session_id=None,
            ctx=ctx,
        )
        assert refresh_calls == [1]

        ctx.access_token_cache.expires_at = 1  # long-past Unix epoch -> stale

        _forward_message(
            client,
            {"jsonrpc": "2.0", "method": "tools/call", "id": 2},
            session_id=None,
            ctx=ctx,
        )

        assert refresh_calls == [2]
        assert captured_headers == ["Bearer refreshed-token-1", "Bearer refreshed-token-2"]


def _config_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    value = os.environ.get("PS_CLI_CONFIG_DIR")
    assert value is not None
    return Path(value)


# --- Group 1: Location -------------------------------------------------------------


def test_main_writes_log_file_under_resolve_config_dir_location(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-001: the log file lands at `<config_dir>/mcp-bridge.log`, per resolve_config_dir()."""
    config_dir = tmp_path_factory.mktemp("config-dir")
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    main(transport=httpx.MockTransport(lambda _req: pytest.fail("no message should be sent")))

    assert (config_dir / _LOG_FILE_NAME).exists()


# --- Group 2: Lifecycle & message logging -------------------------------------------


def test_main_logs_startup_banner_with_pid_ppid_service_url_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002: startup writes one line with PID, parent PID, service URL, context."""
    monkeypatch.setenv("PS_CLI_SERVICE_URL", "https://ps.example.test")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    main(transport=httpx.MockTransport(lambda _req: pytest.fail("no message should be sent")))

    log_path = _config_dir_from_env(monkeypatch) / _LOG_FILE_NAME
    logged = log_path.read_text(encoding="utf-8")
    assert f"pid={os.getpid()}" in logged
    assert f"ppid={os.getppid()}" in logged
    assert "service_url=https://ps.example.test" in logged
    assert "context=None" in logged


def test_main_logs_exit_reason_stdin_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-005: a clean stdin EOF logs an exit line before the process returns."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    main(transport=httpx.MockTransport(lambda _req: pytest.fail("no message should be sent")))

    log_path = _config_dir_from_env(monkeypatch) / _LOG_FILE_NAME
    assert "exiting: stdin closed" in log_path.read_text(encoding="utf-8")


def test_main_logs_unhandled_exception_before_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-006: an unhandled exception in the loop is logged, then still propagates."""
    monkeypatch.setattr("sys.stdin", io.StringIO('{"jsonrpc": "2.0", "method": "x", "id": 1}\n'))

    def _boom(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        main(transport=httpx.MockTransport(_boom))

    log_path = _config_dir_from_env(monkeypatch) / _LOG_FILE_NAME
    logged = log_path.read_text(encoding="utf-8")
    assert "exiting: unhandled exception" in logged
    assert "boom" in logged


def test_forward_message_logs_success_with_method_id_outcome_latency_session(
    tmp_path: Path, keyring_backend: InMemoryKeyringBackend
) -> None:
    """AC-BI-003: a successful forward logs method, id, outcome, latency, session id."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 7, "result": {}},
            headers={"mcp-session-id": "sess-42"},
        )

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, keyring_backend, log_file=log_file)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 7},
        session_id=None,
        ctx=ctx,
    )

    assert reply == {"jsonrpc": "2.0", "id": 7, "result": {}}
    assert session_id == "sess-42"
    logged = log_file.getvalue()
    assert "method=tools/call" in logged
    assert "id=7" in logged
    assert "outcome=ok" in logged
    assert "latency=" in logged
    assert "session=sess-42" in logged


def test_forward_message_logs_transport_failure_with_latency_and_session(
    tmp_path: Path, keyring_backend: InMemoryKeyringBackend
) -> None:
    """AC-BI-004: a transport error is logged (retained + extended) and swallowed."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, keyring_backend, log_file=log_file)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/list", "id": 3},
        session_id="sess-1",
        ctx=ctx,
    )

    assert reply is None
    assert session_id == "sess-1"
    logged = log_file.getvalue()
    assert "request to PS Service failed" in logged
    assert "method=tools/list" in logged
    assert "id=3" in logged
    assert "latency=" in logged
    assert "session=sess-1" in logged


def test_forward_message_returns_generic_error_reply_when_body_is_not_jsonrpc_shaped(
    tmp_path: Path, keyring_backend: InMemoryKeyringBackend
) -> None:
    """AC-BI-004/007: a non-2xx response whose body isn't the JSON-RPC error shape
    PS Service emits still gets a real reply (not silence) -- with a generic,
    status-code-only message, never the raw body text.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, keyring_backend, log_file=log_file)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 9},
        session_id="sess-2",
        ctx=ctx,
    )

    assert reply is not None
    assert reply["id"] == 9
    error = reply["error"]
    assert isinstance(error, dict)
    assert "500" in cast("str", error["message"])
    assert "internal error" not in json.dumps(reply)
    assert session_id == "sess-2"
    logged = log_file.getvalue()
    assert "PS Service returned 500" in logged
    assert "method=tools/call" in logged
    assert "id=9" in logged
    assert "latency=" in logged
    assert "session=sess-2" in logged


def test_forward_message_returns_jsonrpc_error_on_ge400_status_with_upstream_message(
    tmp_path: Path, keyring_backend: InMemoryKeyringBackend
) -> None:
    """AC-BI-004: a >=400 PS Service response for a genuine request returns a real
    JSON-RPC error reply carrying PS Service's own message text -- not silence.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32000, "message": "graph unavailable"},
            },
        )

    ctx = _ctx(tmp_path, keyring_backend)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 11},
        session_id=None,
        ctx=ctx,
    )

    assert reply is not None
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 11
    error = reply["error"]
    assert isinstance(error, dict)
    assert "graph unavailable" in cast("str", error["message"])


def test_forward_message_ge400_for_a_notification_still_returns_no_reply(
    tmp_path: Path, keyring_backend: InMemoryKeyringBackend
) -> None:
    """AC-BI-005: a notification (no `id`) never gets a reply, even on a >=400
    PS Service response -- JSON-RPC 2.0 forbids replying to a notification.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Session not found"},
            },
        )

    ctx = _ctx(tmp_path, keyring_backend)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        session_id="sess-4",
        ctx=ctx,
    )

    assert reply is None


def test_forward_message_session_not_found_error_distinguishes_expired_session(
    tmp_path: Path, keyring_backend: InMemoryKeyringBackend
) -> None:
    """AC-BI-004/006: PS Service's literal "Session not found" (mcp SDK's fixed text
    for an unknown/expired session id -- e.g. after a pod restart wiped in-memory
    session state) is rewrapped into a message that names it as an expired/reset
    session, while still carrying PS Service's own original text.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Session not found"},
            },
        )

    ctx = _ctx(tmp_path, keyring_backend)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 13},
        session_id="sess-5",
        ctx=ctx,
    )

    assert reply is not None
    assert reply["id"] == 13
    error = reply["error"]
    assert isinstance(error, dict)
    message = cast("str", error["message"])
    assert "Session not found" in message  # AC-BI-004: carries PS Service's own text
    assert "expired" in message.lower() or "restart" in message.lower()  # AC-BI-006


def test_forward_message_ge400_error_never_leaks_bearer_token_or_raw_body(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """AC-BI-007: the >=400 error reply never includes the bearer token or PS
    Service's raw response body -- only the sanitized, extracted message text.
    """
    ctx = _ctx_with_cached_access_token(
        keyring_backend, context_name="prod", access_token="sk-topsecret-access-token"
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-topsecret-access-token"
        return httpx.Response(500, text="stacktrace: /var/secrets/db-password=hunter2")

    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 17},
        session_id=None,
        ctx=ctx,
    )

    assert reply is not None
    dumped = json.dumps(reply)
    assert "sk-topsecret-access-token" not in dumped
    assert "hunter2" not in dumped
    assert "/var/secrets" not in dumped


def test_forward_message_logs_token_resolution_failure_with_latency_and_session(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """AC-BI-004: a token-resolution failure (no refresh token stored) is logged.

    Issue #119, AC-BI-005/007: it is no longer swallowed into a silent no-reply --
    `reply`'s own shape is covered separately below.
    """
    log_file = io.StringIO()
    ctx = _ctx_with_stale_bundle(keyring_backend, context_name="prod", log_file=log_file)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _req: pytest.fail("must not reach PS Service"))
    )

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 5},
        session_id="sess-3",
        ctx=ctx,
    )

    assert reply is not None
    assert session_id == "sess-3"
    logged = log_file.getvalue()
    assert "could not get access token" in logged
    assert "method=tools/call" in logged
    assert "id=5" in logged
    assert "latency=" in logged
    assert "session=sess-3" in logged


def test_forward_message_returns_jsonrpc_error_on_token_resolution_failure(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """Issue #119, AC-BI-005/006/007: a token-resolution failure returns a real
    JSON-RPC error reply -- not a silent no-reply -- so the MCP host (Claude
    Desktop) surfaces the actual cause instead of a generic timeout. Carries the
    original request's own `id`, and never a token value.
    """
    ctx = _ctx_with_stale_bundle(keyring_backend, context_name="prod")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _req: pytest.fail("must not reach PS Service"))
    )

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 5},
        session_id="sess-3",
        ctx=ctx,
    )

    assert reply is not None
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 5
    error = reply["error"]
    assert isinstance(error, dict)
    assert "stored credentials could not be refreshed" in cast("str", error["message"])
    assert "ps-cli auth login" in cast("str", error["message"])


def test_forward_message_token_resolution_failure_for_a_notification_still_returns_no_reply(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """Issue #119, AC-BI-005: a notification (no `id`) never gets a reply, even on a
    token failure -- JSON-RPC 2.0 forbids replying to a notification; only the log
    line (asserted above) carries the failure for that case.
    """
    ctx = _ctx_with_stale_bundle(keyring_backend, context_name="prod")
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _req: pytest.fail("must not reach PS Service"))
    )

    reply, _ = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        session_id=None,
        ctx=ctx,
    )

    assert reply is None


# --- Group 3: Resilience -------------------------------------------------------------


def test_log_file_is_appended_across_restarts_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-007: restarting against the same config_dir appends, never truncates."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    transport = httpx.MockTransport(lambda _req: pytest.fail("no message should be sent"))

    main(transport=transport)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    main(transport=transport)

    log_path = _config_dir_from_env(monkeypatch) / _LOG_FILE_NAME
    logged = log_path.read_text(encoding="utf-8")
    assert logged.count("starting:") == 2
    assert logged.count("exiting: stdin closed") == 2


def test_open_log_file_failure_falls_back_to_none_and_warns_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-BI-008: a log file that can't be opened doesn't raise -- warns and returns None."""
    # A regular file where a directory is expected: `mkdir(parents=True, exist_ok=True)`
    # can't paper over that, unlike a merely-missing parent directory (which it creates).
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    unwritable_path = blocked_parent / _LOG_FILE_NAME

    log_file = _open_log_file(unwritable_path)

    assert log_file is None
    assert "could not open log file" in capsys.readouterr().err


def test_main_still_proxies_when_log_file_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-BI-008: the bridge still starts and proxies messages when logging setup fails."""
    # A regular file where a directory is expected breaks `config_dir / _LOG_FILE_NAME`.
    blocked_config_dir = tmp_path / "blocked-config-dir"
    blocked_config_dir.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("PS_CLI_CONFIG_DIR", str(blocked_config_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"jsonrpc": "2.0", "method": "x", "id": 1}\n'))

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    main(transport=httpx.MockTransport(_handler))
    # No assertion beyond "did not raise" -- a logging failure must never break the proxy.


# --- Group 4: Security ----------------------------------------------------------------


def test_forward_message_never_logs_message_params_or_bearer_token(
    keyring_backend: InMemoryKeyringBackend,
) -> None:
    """AC-BI-009: log lines never include the bearer token or the message body/params."""
    log_file = io.StringIO()
    ctx = _ctx_with_cached_access_token(
        keyring_backend,
        context_name="prod",
        access_token="sk-topsecret-access-token",
        log_file=log_file,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-topsecret-access-token"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    client = httpx.Client(transport=httpx.MockTransport(_handler))

    _forward_message(
        client,
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {"secret": "super-sensitive-payload"},
        },
        session_id=None,
        ctx=ctx,
    )

    logged = log_file.getvalue()
    assert "sk-topsecret-access-token" not in logged
    assert "super-sensitive-payload" not in logged


def test_open_log_file_creates_file_with_non_world_readable_permissions(tmp_path: Path) -> None:
    """AC-BI-010: a newly created log file is not group/other readable."""
    path = tmp_path / _LOG_FILE_NAME

    log_file = _open_log_file(path)

    assert log_file is not None
    log_file.close()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0


# --- _log() itself ---------------------------------------------------------------------


def test_log_writes_to_both_stderr_and_log_file(capsys: pytest.CaptureFixture[str]) -> None:
    """`_log` writes the same line to stderr (always) and the log file (when given)."""
    log_file = io.StringIO()

    _log("hello world", log_file=log_file)

    assert "hello world" in capsys.readouterr().err
    assert "hello world" in log_file.getvalue()
