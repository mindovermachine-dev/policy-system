"""End-to-end MCP auth tests (issue #58, Slice 5: AC-BI-006, AC-BI-007).

Drives the *real* mounted Streamable HTTP ASGI app -- the same
`build_streamable_http_app` production code path `ps_service.main.create_app`
uses -- through a real `TestClient`, against the shared `MockOidcProvider`
test infra (`tests/auth/mock_oidc_provider.py`, AC-BI-017: generic, no
Auth0/Entra shape). No second, MCP-specific mock IdP.

AC-BI-006: an MCP request to `/mcp` with no/invalid token is rejected by the
same `PsTokenVerifier`, via the MCP SDK's own `MCPServer(token_verifier=...)`
gate (`RequireAuthMiddleware`/`BearerAuthBackend`) -- not a reimplementation
in this codebase.

AC-BI-007: under a valid token, the `cypher` tool's `principal` is the
token's `sub` -- never `LOCAL_TEST_PRINCIPAL_ID`, never `None`. Proven by
monkeypatching `execute_cypher_query` (mirrors `test_cypher_tool.py`'s own
spy convention) to capture the `principal=` keyword `handle_mcp_tool_call`
threads straight through, rather than reimplementing a fake `AccessToken`
context -- the real `get_access_token()` contextvar, populated by the SDK's
own `AuthContextMiddleware`, is what supplies it end to end.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, cast

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from tests.auth.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.http_transport import (
    MCP_HTTP_MOUNT_PATH,
    build_streamable_http_app,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest
    from tests.auth.mock_oidc_provider import MockOidcProvider

    from ps_service.logging.emitter import LogEmitter
    from ps_service.query_engine.falkordb_client import GraphHandle
    from ps_service.query_engine.models import QueryResult

_BASE_URL = "http://127.0.0.1:8000"
_JSON_RPC_ACCEPT = "application/json, text/event-stream"
_ALLOWED_ALGORITHMS = frozenset({"RS256"})


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally with scripted values."""

    def __init__(self, *, header: list[list[object]], result_set: list[object]) -> None:
        self.header = header
        self.result_set = result_set


class _FakeGraphHandle:
    """Satisfies `GraphHandle` structurally, reporting itself as seeded."""

    def query(
        self, q: str, params: dict[str, object] | None = None, timeout: int | None = None
    ) -> _FakeQueryResult:
        return _FakeQueryResult(header=[[0, "c"]], result_set=[[1]])


class _FakeFalkorDB:
    """Stands in for the eager `falkordb.FalkorDB` client."""

    def __init__(self, handle: _FakeGraphHandle) -> None:
        self._handle = handle

    def select_graph(self, name: str) -> _FakeGraphHandle:
        return self._handle


def _auth_context(provider: MockOidcProvider) -> AuthContext:
    return AuthContext(
        issuer=provider.issuer,
        audience="ps-service",
        cli_client_id=None,
        scopes=(),
        jwks_uri=provider.jwks_uri,
        allowed_algorithms=_ALLOWED_ALGORITHMS,
    )


def _authenticated_test_client(*, auth_context: AuthContext, host: str = "127.0.0.1") -> TestClient:
    """A `TestClient` over the real, auth-armed `build_streamable_http_app`.

    Mirrors `test_http_transport.py::_wrapped_test_client` exactly, except
    it threads a real `PsTokenVerifier`/`AuthContext` pair through, so the
    mounted app is the genuinely auth-enforcing one (AC-BI-006), not the
    bypass-shaped one every other MCP Interface test builds.
    """
    verifier = PsTokenVerifier(auth_context)
    mcp_asgi_app = build_streamable_http_app(
        host=host, verifier=verifier, auth_context=auth_context
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
            yield

    wrapper_app = Starlette(lifespan=lifespan)
    wrapper_app.mount(MCP_HTTP_MOUNT_PATH, mcp_asgi_app)
    return TestClient(wrapper_app, base_url=_BASE_URL)


def test_mcp_request_with_no_authorization_header_is_rejected_with_401(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """AC-BI-006, proven for real: no `Authorization` header at all against
    the genuinely auth-armed app -- the MCP SDK's own `RequireAuthMiddleware`
    rejects it before the `initialize` handshake ever completes, via the
    same shared `PsTokenVerifier` REST uses (never a parallel gate).
    """
    with _authenticated_test_client(auth_context=_auth_context(mock_oidc_provider)) as client:
        response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": _JSON_RPC_ACCEPT},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer ")


def test_mcp_request_with_an_invalid_token_is_rejected_with_401(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """AC-BI-006: a syntactically-`Bearer`-shaped but unverifiable token
    (wrong audience) is rejected the same way as no token at all -- proving
    the SDK's gate genuinely calls into `PsTokenVerifier.verify_token`,
    not merely checking header shape.
    """
    # Slice 10: unlike the no-header case above (rejected by the SDK's own gate
    # before `verify_token` is ever called), a present-but-invalid token does
    # reach `PsTokenVerifier.verify_token`, which now always logs
    # (AC-BI-014/015) -- a configured facade is required (`tests/conftest.py`'s
    # autouse `_isolate_logging` redirects `PS_LOGGING_DIR` and resets after).
    configure()
    auth_context = _auth_context(mock_oidc_provider)
    bad_token = mock_oidc_provider.mint_token(aud="some-other-audience")

    with _authenticated_test_client(auth_context=auth_context) as client:
        response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": _JSON_RPC_ACCEPT, "Authorization": f"Bearer {bad_token}"},
        )

    assert response.status_code == 401


def test_cypher_tool_principal_is_the_valid_tokens_sub(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider
) -> None:
    """AC-BI-007: under a valid token, the `cypher` tool's `principal` is
    exactly the token's `sub` -- never `LOCAL_TEST_PRINCIPAL_ID`, never
    `None`. Drives the real mounted transport end to end (`initialize` ->
    `notifications/initialized` -> `tools/call`), the same JSON-RPC sequence
    `test_http_transport.py` already establishes, now with a real
    `Authorization: Bearer <token>` header on every request.
    """
    configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-42"
    token = mock_oidc_provider.mint_token(sub=token_sub)

    handle = _FakeGraphHandle()
    fake_db = _FakeFalkorDB(handle)

    def _connect_from_config(_config: object) -> _FakeFalkorDB:
        return fake_db

    monkeypatch.setattr(mcp_server, "connect_from_config", _connect_from_config)

    calls: list[dict[str, object]] = []
    real_execute = mcp_server.execute_cypher_query

    def _spy(
        query: str,
        *,
        graph: GraphHandle,
        emitter: LogEmitter | None = None,
        principal: str | None = None,
        timeout_ms: int,
        row_cap: int,
    ) -> QueryResult:
        calls.append({"principal": principal})
        return real_execute(
            query,
            graph=graph,
            emitter=emitter,
            principal=principal,
            timeout_ms=timeout_ms,
            row_cap=row_cap,
        )

    monkeypatch.setattr(mcp_server, "execute_cypher_query", _spy)

    with _authenticated_test_client(auth_context=auth_context) as client:
        init_response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-mcp-auth-client", "version": "0.0.1"},
                },
            },
            headers={"Accept": _JSON_RPC_ACCEPT, "Authorization": f"Bearer {token}"},
        )
        assert init_response.status_code == 200
        session_id = init_response.headers["mcp-session-id"]

        notified = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={
                "Accept": _JSON_RPC_ACCEPT,
                "Authorization": f"Bearer {token}",
                "mcp-session-id": session_id,
            },
        )
        assert notified.status_code == 202

        call_response = client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "cypher", "arguments": {"query": "MATCH (n) RETURN n"}},
            },
            headers={
                "Accept": _JSON_RPC_ACCEPT,
                "Authorization": f"Bearer {token}",
                "mcp-session-id": session_id,
            },
        )

    assert call_response.status_code == 200
    result_text = _sse_result_text(call_response.text)
    assert json.loads(result_text)["row_count"] == 1
    assert len(calls) == 1
    assert calls[0]["principal"] == token_sub


def _sse_result_text(response_text: str) -> str:
    """Extract the tool call's text content out of an SSE-formatted response body."""
    for line in response_text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line.removeprefix("data:").strip())
            result = cast("dict[str, object]", payload["result"])
            content = cast("list[object]", result["content"])
            block = cast("dict[str, object]", content[0])
            text = block["text"]
            assert isinstance(text, str)
            return text
    msg = f"no 'data:' line found in SSE body: {response_text!r}"
    raise AssertionError(msg)
