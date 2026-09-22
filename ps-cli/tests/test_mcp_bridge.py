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

from ps_cli.credentials import FileCredentialStore, TokenBundle
from ps_cli.mcp_bridge import (
    _LOG_FILE_NAME,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _BridgeContext,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _forward_message,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _log,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    _open_log_file,  # pyright: ignore[reportPrivateUsage]  # unit-tested directly, see module docstring
    main,
)

if TYPE_CHECKING:
    from typing import TextIO

_MCP_URL = "https://ps.example.test/mcp/"


def _ctx(
    tmp_path: Path,
    *,
    context_name: str | None = None,
    log_file: TextIO | None = None,
) -> _BridgeContext:
    """Build a `_BridgeContext` with literal values -- no real config/service needed."""
    return _BridgeContext(
        mcp_url=_MCP_URL,
        context_name=context_name,
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=FileCredentialStore(tmp_path),
        log_file=log_file,
    )


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
    tmp_path: Path,
) -> None:
    """AC-BI-003: a successful forward logs method, id, outcome, latency, session id."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 7, "result": {}},
            headers={"mcp-session-id": "sess-42"},
        )

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, log_file=log_file)
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
    tmp_path: Path,
) -> None:
    """AC-BI-004: a transport error is logged (retained + extended) and swallowed."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, log_file=log_file)
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


def test_forward_message_logs_non_2xx_with_latency_and_session(tmp_path: Path) -> None:
    """AC-BI-004: a non-2xx response is logged (retained + extended) and swallowed."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    log_file = io.StringIO()
    ctx = _ctx(tmp_path, log_file=log_file)
    client = httpx.Client(transport=httpx.MockTransport(_handler))

    reply, session_id = _forward_message(
        client,
        {"jsonrpc": "2.0", "method": "tools/call", "id": 9},
        session_id="sess-2",
        ctx=ctx,
    )

    assert reply is None
    assert session_id == "sess-2"
    logged = log_file.getvalue()
    assert "PS Service returned 500" in logged
    assert "method=tools/call" in logged
    assert "id=9" in logged
    assert "latency=" in logged
    assert "session=sess-2" in logged


def test_forward_message_logs_token_resolution_failure_with_latency_and_session(
    tmp_path: Path,
) -> None:
    """AC-BI-004: a token-resolution failure (expired, no refresh token) is logged.

    Issue #119, AC-BI-005/007: it is no longer swallowed into a silent no-reply --
    `reply`'s own shape is covered separately below.
    """
    store = FileCredentialStore(tmp_path)
    store.set_tokens(
        "prod",
        TokenBundle(
            access_token="stale", refresh_token=None, expires_at=0, issuer="https://idp.example"
        ),
    )
    log_file = io.StringIO()
    ctx = _BridgeContext(
        mcp_url=_MCP_URL,
        context_name="prod",
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=store,
        log_file=log_file,
    )
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
    tmp_path: Path,
) -> None:
    """Issue #119, AC-BI-005/006/007: a token-resolution failure returns a real
    JSON-RPC error reply -- not a silent no-reply -- so the MCP host (Claude
    Desktop) surfaces the actual cause instead of a generic timeout. Carries the
    original request's own `id`, and never a token value.
    """
    store = FileCredentialStore(tmp_path)
    store.set_tokens(
        "prod",
        TokenBundle(
            access_token="sk-should-never-appear-in-a-reply",
            refresh_token=None,
            expires_at=0,
            issuer="https://idp.example",
        ),
    )
    ctx = _BridgeContext(
        mcp_url=_MCP_URL,
        context_name="prod",
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=store,
        log_file=None,
    )
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
    assert "sk-should-never-appear-in-a-reply" not in json.dumps(reply)


def test_forward_message_token_resolution_failure_for_a_notification_still_returns_no_reply(
    tmp_path: Path,
) -> None:
    """Issue #119, AC-BI-005: a notification (no `id`) never gets a reply, even on a
    token failure -- JSON-RPC 2.0 forbids replying to a notification; only the log
    line (asserted above) carries the failure for that case.
    """
    store = FileCredentialStore(tmp_path)
    store.set_tokens(
        "prod",
        TokenBundle(
            access_token="stale", refresh_token=None, expires_at=0, issuer="https://idp.example"
        ),
    )
    ctx = _BridgeContext(
        mcp_url=_MCP_URL,
        context_name="prod",
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=store,
        log_file=None,
    )
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


def test_forward_message_never_logs_message_params_or_bearer_token(tmp_path: Path) -> None:
    """AC-BI-009: log lines never include the bearer token or the message body/params."""
    store = FileCredentialStore(tmp_path)
    store.set_tokens(
        "prod",
        TokenBundle(
            access_token="sk-topsecret-access-token",
            refresh_token=None,
            expires_at=99999999999,
            issuer="https://idp.example",
        ),
    )
    log_file = io.StringIO()
    ctx = _BridgeContext(
        mcp_url=_MCP_URL,
        context_name="prod",
        service_url="https://ps.example.test",
        auth_override=None,
        credential_store=store,
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
