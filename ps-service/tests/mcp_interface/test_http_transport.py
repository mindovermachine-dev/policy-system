"""Tests for `ps_service.mcp_interface.http_transport` (issue #39, Slice 3).

Proves `build_streamable_http_app` wraps the *same* `mcp_server.server`
instance the stdio entrypoint uses in a real Streamable HTTP ASGI app: the
`cypher` tool and the `psdomain://concepts` resource are reachable over real
JSON-RPC-over-HTTP, not a reimplementation. Driven end-to-end through
`TestClient` -- a real ASGI request/response cycle, headers included -- per
PLAN.md §5 Slice 3, with the mount's own `lifespan` entered explicitly
(PLAN.md §1 F2: mounting alone does not start the SDK's session manager).

Per CHANGES.md F-3, this file intentionally has only 5 tests, not the 6
PLAN.md's Slice 3 draft describes:
`test_no_token_verifier_or_auth_configured_by_default` (asserting
`RequireAuthMiddleware not in app.middleware`) was dropped -- verified
against the SDK's own route-construction code, that assertion can never
fail (`RequireAuthMiddleware` wraps the route's `endpoint=` directly, never
appended to `.middleware`), so it would be a no-op, not real coverage. The
real "no auth silently wired" seam is
`tests/mcp_interface/test_scope_guard.py::test_mcpserver_ctor_has_no_auth_kwargs`,
already covering `mcp_server.py`'s sole `MCPServer(...)` constructor call --
untouched by this issue.

Hand-written structural fakes (mirrors `test_cypher_tool.py`'s
`_FakeGraphHandle`/`_FakeFalkorDB`) and monkeypatching only -- no
`unittest.mock`. Every request against the mounted transport includes an
explicit port in its base URL (PLAN.md §1 F3): the SDK's DNS-rebinding
protection auto-enables for loopback hosts and rejects a `Host` header with
no port.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.http_transport import (
    MCP_HTTP_MOUNT_PATH,
    build_streamable_http_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

_BASE_URL = "http://127.0.0.1:8000"
_JSON_RPC_ACCEPT = "application/json, text/event-stream"


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted values."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally, returning a scripted result."""

    def __init__(self, *, result: _FakeQueryResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        return self._result


class _FakeFalkorDB:
    """Stands in for the eager `falkordb.FalkorDB` client."""

    def __init__(self, handle: _FakeGraphHandle) -> None:
        self._handle = handle
        self.selected: list[str] = []

    def select_graph(self, name: str) -> _FakeGraphHandle:
        self.selected.append(name)
        return self._handle


def _install_graph(monkeypatch: pytest.MonkeyPatch, handle: _FakeGraphHandle) -> None:
    fake_db = _FakeFalkorDB(handle)

    def _connect_from_config(_config: object) -> _FakeFalkorDB:
        return fake_db

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect_from_config)


def _wrapped_test_client(*, host: str = "127.0.0.1") -> TestClient:
    """A `TestClient` over `build_streamable_http_app`, its own lifespan entered.

    `app.mount(...)` alone never starts the mounted sub-app's session
    manager (PLAN.md §1 F2) -- the outer wrapper's `lifespan` must enter
    `mcp_asgi_app.router.lifespan_context(mcp_asgi_app)` explicitly.
    """
    mcp_asgi_app = build_streamable_http_app(host=host)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
            yield

    wrapper_app = Starlette(lifespan=lifespan)
    wrapper_app.mount(MCP_HTTP_MOUNT_PATH, mcp_asgi_app)
    return TestClient(wrapper_app, base_url=_BASE_URL)


def _as_dict(value: object) -> dict[str, object]:
    """Narrow an already-`isinstance`-checked JSON value to `dict[str, object]`."""
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _as_list(value: object) -> list[object]:
    """Narrow an already-`isinstance`-checked JSON value to `list[object]`."""
    assert isinstance(value, list)
    return cast("list[object]", value)


def _sse_result(response_text: str) -> dict[str, object]:
    """Extract the JSON-RPC `result` object from an SSE-formatted response body."""
    for line in response_text.splitlines():
        if line.startswith("data:"):
            payload: object = json.loads(line.removeprefix("data:").strip())
            payload_dict = _as_dict(payload)
            return _as_dict(payload_dict["result"])
    pytest.fail(f"no 'data:' line found in SSE body: {response_text!r}")


def _initialize_session(client: TestClient) -> str:
    """Drive `initialize` -> `notifications/initialized`, returning the session id."""
    response = client.post(
        f"{MCP_HTTP_MOUNT_PATH}/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-http-transport-client", "version": "0.0.1"},
            },
        },
        headers={"Accept": _JSON_RPC_ACCEPT},
    )
    assert response.status_code == 200
    session_id = response.headers["mcp-session-id"]

    notified = client.post(
        f"{MCP_HTTP_MOUNT_PATH}/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Accept": _JSON_RPC_ACCEPT, "mcp-session-id": session_id},
    )
    assert notified.status_code == 202
    return session_id


def test_build_streamable_http_app_returns_a_starlette_app() -> None:
    app = build_streamable_http_app(host="127.0.0.1")

    assert isinstance(app, Starlette)


def test_mount_path_constant_has_a_stable_value() -> None:
    assert MCP_HTTP_MOUNT_PATH == "/mcp"


def test_cypher_tool_reachable_through_the_built_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors `test_cypher_tool.py::test_success_via_call_tool_returns_json_content`,
    now proving the *same* registered tool is reachable over the HTTP transport.
    """
    configure()
    handle = _FakeGraphHandle(
        result=_FakeQueryResult(
            header=[[0, "id"], [0, "name"]],
            result_set=[["a", "Alice"], ["b", "Bob"]],
        )
    )
    _install_graph(monkeypatch, handle)

    with _wrapped_test_client() as client:
        session_id = _initialize_session(client)

        response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "cypher",
                    "arguments": {"query": "MATCH (n) RETURN n.id, n.name"},
                },
            },
            headers={"Accept": _JSON_RPC_ACCEPT, "mcp-session-id": session_id},
        )

    assert response.status_code == 200
    result = _sse_result(response.text)
    assert result["isError"] is False
    content = _as_list(result["content"])
    text_block = _as_dict(content[0])
    text = text_block["text"]
    assert isinstance(text, str)
    assert json.loads(text) == {
        "columns": ["id", "name"],
        "rows": [["a", "Alice"], ["b", "Bob"]],
        "row_count": 2,
    }


def test_domain_concepts_resource_reachable_through_the_built_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors `test_domain_concepts_resource.py`'s monkeypatch shape, now
    proving the resource is reachable over the HTTP transport.
    """
    md_file = tmp_path / "ps-domain-concepts.md"
    md_file.write_text("# PS domain concepts\n\nRegulation -> Obligation\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_domain_concepts_path", lambda: md_file)

    with _wrapped_test_client() as client:
        session_id = _initialize_session(client)

        response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "psdomain://concepts"},
            },
            headers={"Accept": _JSON_RPC_ACCEPT, "mcp-session-id": session_id},
        )

    assert response.status_code == 200
    result = _sse_result(response.text)
    contents = _as_list(result["contents"])
    first = _as_dict(contents[0])
    assert first["text"] == md_file.read_text(encoding="utf-8")
    assert first["mimeType"] == "text/markdown"


def test_mismatched_host_header_is_rejected() -> None:
    """PLAN.md §1 F3: the SDK's DNS-rebinding protection rejects a `Host`
    header naming neither the configured host nor one of the recognized
    loopback spellings, with the port `_wrapped_test_client` always sends.
    """
    with _wrapped_test_client() as client:
        response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": _JSON_RPC_ACCEPT, "Host": "evil.example.com"},
        )

    assert response.status_code == 421
