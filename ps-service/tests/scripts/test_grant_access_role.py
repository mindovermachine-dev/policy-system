"""End-to-end tests for `scripts/grant-access-role.sh` (issue #174).

The script runs for real as a subprocess against a real stdlib `http.server` on 127.0.0.1 that
speaks PS Service's Streamable HTTP MCP shape (`/mcp/`: initialize -> session id,
`notifications/initialized` -> 202, `tools/call` -> a scripted reply). The bearer token is seeded
only through `GRANT_ACCESS_ROLE_TOKEN`; the server records what it saw so the tests can assert
the `Authorization` header, the session echo and the tool arguments. Nothing is patched.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "grant-access-role.sh"

SUBPROCESS_TIMEOUT_SECONDS = 120.0
TOKEN = "tok-123"  # fixture bearer value, asserted never to leak
SESSION_ID = "sess-1"


@dataclass
class Recorded:
    """One request the fake PS Service received."""

    headers: dict[str, str]
    body: dict[str, object]


@dataclass
class FakePsService:
    """Scripted behaviour for the `tools/call` reply, plus what the server saw."""

    mode: str = "success"
    requests: list[Recorded] = field(default_factory=list)

    def tool_result(self, text: str, request_id: object) -> bytes:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        ).encode()


class _Handler(BaseHTTPRequestHandler):
    service: ClassVar[FakePsService]

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet."""

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status == 200 and b'"protocolVersion"' in body:
            self.send_header("Mcp-Session-Id", SESSION_ID)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body: dict[str, object] = json.loads(self.rfile.read(length))
        service = self.service
        service.requests.append(
            Recorded(headers={k.lower(): v for k, v in self.headers.items()}, body=body)
        )
        method = body.get("method")
        if method == "initialize":
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}}
            ).encode()
            self._send(200, payload)
        elif method == "notifications/initialized":
            self._send(202)
        elif service.mode == "unauthorized":
            self._send(401, b'{"error":"unauthorized"}')
        elif service.mode == "server-error":
            self._send(500, b"boom")
        elif service.mode == "tool-error-text":
            self._send(200, service.tool_result("error: caller is not a SystemOwner", body["id"]))
        elif service.mode == "sse":
            data = service.tool_result("granted", body["id"]).decode()
            self._send(200, f"event: message\ndata: {data}\n\n".encode(), "text/event-stream")
        else:
            self._send(200, service.tool_result("granted", body["id"]))


@dataclass
class Running:
    service: FakePsService
    url: str


@pytest.fixture
def ps_service() -> Iterator[Running]:
    service = FakePsService()
    handler = type("Handler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Running(service=service, url=f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run(
    tmp_path: Path, url: str, *args: str, token: str | None = TOKEN
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "GRANT_ACCESS_ROLE_TOKEN"}
    env["PS_CLI_SERVICE_URL"] = url
    env["PS_CLI_CONFIG_DIR"] = str(tmp_path)
    if token is not None:
        env["GRANT_ACCESS_ROLE_TOKEN"] = token
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )


def _assert_no_token_leak(result: subprocess.CompletedProcess[str]) -> None:
    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr


def test_grant_succeeds_with_bearer_session_echo_and_flag_arguments(
    ps_service: Running, tmp_path: Path
) -> None:
    result = _run(tmp_path, ps_service.url)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Granted ComplianceOfficer to tete@cartman.dk\n")
    seen = ps_service.service.requests
    assert [r.body.get("method") for r in seen] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in seen)
    assert "mcp-session-id" not in seen[0].headers
    assert seen[1].headers["mcp-session-id"] == SESSION_ID
    assert seen[2].headers["mcp-session-id"] == SESSION_ID
    assert seen[2].body["params"] == {
        "name": "grant-access-role",
        "arguments": {"principal_subject": "tete@cartman.dk", "access_role": "ComplianceOfficer"},
    }
    _assert_no_token_leak(result)


def test_grant_passes_the_principal_and_role_flags_through(
    ps_service: Running, tmp_path: Path
) -> None:
    result = _run(
        tmp_path, ps_service.url, "--principal", "ann@example.com", "--role", "SystemAdmin"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Granted SystemAdmin to ann@example.com\n")
    assert ps_service.service.requests[2].body["params"] == {
        "name": "grant-access-role",
        "arguments": {"principal_subject": "ann@example.com", "access_role": "SystemAdmin"},
    }


def test_without_a_token_or_stored_credential_no_authorization_header_is_sent(
    ps_service: Running, tmp_path: Path
) -> None:
    result = _run(tmp_path, ps_service.url, token=None)

    assert result.returncode == 0, result.stderr
    assert all("authorization" not in r.headers for r in ps_service.service.requests)


def test_tool_error_text_exits_one_with_grant_failed(ps_service: Running, tmp_path: Path) -> None:
    ps_service.service.mode = "tool-error-text"

    result = _run(tmp_path, ps_service.url)

    assert result.returncode == 1
    assert "Grant failed: error: caller is not a SystemOwner" in result.stderr
    _assert_no_token_leak(result)


def test_event_stream_reply_is_understood(ps_service: Running, tmp_path: Path) -> None:
    ps_service.service.mode = "sse"

    result = _run(tmp_path, ps_service.url)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Granted ComplianceOfficer to tete@cartman.dk\n")


def test_unauthorized_exits_one_and_points_at_auth_login(
    ps_service: Running, tmp_path: Path
) -> None:
    ps_service.service.mode = "unauthorized"

    result = _run(tmp_path, ps_service.url)

    assert result.returncode == 1
    assert "ps-cli auth login" in result.stderr
    _assert_no_token_leak(result)


def test_server_error_exits_one_naming_the_status(ps_service: Running, tmp_path: Path) -> None:
    ps_service.service.mode = "server-error"

    result = _run(tmp_path, ps_service.url)

    assert result.returncode == 1
    assert "HTTP 500" in result.stderr
    _assert_no_token_leak(result)


def test_unreachable_service_exits_one_saying_cannot_reach(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    result = _run(tmp_path, f"http://127.0.0.1:{closed_port}")

    assert result.returncode == 1
    assert "Cannot reach" in result.stderr
    _assert_no_token_leak(result)


def test_invalid_role_exits_two_without_calling_the_service(
    ps_service: Running, tmp_path: Path
) -> None:
    result = _run(tmp_path, ps_service.url, "--role", "bogus")

    assert result.returncode == 2
    assert "Invalid --role bogus" in result.stderr
    assert ps_service.service.requests == []


def test_unknown_argument_exits_two(ps_service: Running, tmp_path: Path) -> None:
    result = _run(tmp_path, ps_service.url, "--nope")

    assert result.returncode == 2
    assert "Unknown argument: --nope" in result.stderr


def test_help_prints_usage_and_exits_zero(ps_service: Running, tmp_path: Path) -> None:
    result = _run(tmp_path, ps_service.url, "--help")

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "--principal" in result.stdout
    assert "GRANT_ACCESS_ROLE_TOKEN" in result.stdout
    assert ps_service.service.requests == []
