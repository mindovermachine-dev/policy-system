"""End-to-end MCP auth tests .

Drives the *real* mounted Streamable HTTP ASGI app -- the same
`build_streamable_http_app` production code path `ps_service.main.create_app`
uses -- through a real `TestClient`, against the shared `MockOidcProvider`
test infra (`tests/auth/mock_oidc_provider.py`: generic, no
provider-specific shape). No second, MCP-specific mock IdP.

An MCP request to `/mcp` with no/invalid token is rejected by the
same `PsTokenVerifier`, via the MCP SDK's own `MCPServer(token_verifier=...)`
gate (`RequireAuthMiddleware`/`BearerAuthBackend`) -- not a reimplementation
in this codebase.

Under a valid token, the `run-cypher` tool's `query_engine` log entry carries no principal and the
token's `sub` reaches no log line, while the tool's `query.cypher` audit row records that `sub` as
its actor. Proven by reading the real `query_engine`/`execute_cypher_query` log entry the real,
unmonkeypatched delegate emits end to end, and the row an in-memory audit store receives. No fake
`AccessToken` context is built: the real `get_access_token()` contextvar, populated by the SDK's
own `AuthContextMiddleware`, is what supplies the actor end to end.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, cast

from audit._fakes import InMemoryAuditStore, audit_store_factory
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from mcp_interface._environment import graph_factory_for
from mcp_interface.test_access_role_tools import (
    _override,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
    _server,  # pyright: ignore[reportPrivateUsage]  # reuse the access-role tool tests' override helpers
)
from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface.http_transport import (
    MCP_HTTP_MOUNT_PATH,
    build_streamable_http_app,
)
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    import pytest
    from httpx2 import Response

    from ps_test_support.mock_oidc_provider import MockOidcProvider

    type ReadLines = Callable[[Path], list[dict[str, object]]]

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
    mounted app is the genuinely auth-enforcing one, not the
    bypass-shaped one every other MCP Interface test builds.
    """
    verifier = PsTokenVerifier(auth_context)
    mcp_asgi_app = build_streamable_http_app(
        server=_server(), host=host, verifier=verifier, auth_context=auth_context
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
            yield

    wrapper_app = Starlette(lifespan=lifespan)
    wrapper_app.mount(MCP_HTTP_MOUNT_PATH, mcp_asgi_app)
    return TestClient(wrapper_app, base_url=_BASE_URL)


def _serve_a_seeded_fake_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the `run-cypher` tool's FalkorDB connection at a seeded fake graph."""
    fake_db = _FakeFalkorDB(_FakeGraphHandle())

    _override(monkeypatch, graph_factory=graph_factory_for(fake_db))


def _call_cypher_tool_over_bearer_http(*, auth_context: AuthContext, token: str) -> Response:
    """Drive `initialize` -> `notifications/initialized` -> `tools/call cypher` with `token`.

    Every request carries `Authorization: Bearer <token>` against the auth-armed app; returns the
    `tools/call` response.
    """
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

        return client.post(
            f"{MCP_HTTP_MOUNT_PATH}/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "run-cypher", "arguments": {"query": "MATCH (n) RETURN n"}},
            },
            headers={
                "Accept": _JSON_RPC_ACCEPT,
                "Authorization": f"Bearer {token}",
                "mcp-session-id": session_id,
            },
        )


def test_mcp_request_with_no_authorization_header_is_rejected_with_401(
    mock_oidc_provider: MockOidcProvider,
) -> None:
    """Proven for real: no `Authorization` header at all against
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
    """A syntactically-`Bearer`-shaped but unverifiable token
    (wrong audience) is rejected the same way as no token at all -- proving
    the SDK's gate genuinely calls into `PsTokenVerifier.verify_token`,
    not merely checking header shape.
    """
    # Unlike the no-header case above (rejected by the SDK's own gate
    # before `verify_token` is ever called), a present-but-invalid token does
    # reach `PsTokenVerifier.verify_token`, which always logs
    # -- a configured facade is required (`tests/conftest.py`'s
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


def test_bearer_authenticated_cypher_call_logs_no_principal_and_audits_the_actor(
    monkeypatch: pytest.MonkeyPatch,
    mock_oidc_provider: MockOidcProvider,
    read_lines: ReadLines,
) -> None:
    """A bearer-authenticated `run-cypher` call logs its `query_engine` entry with no `principal`
    key and writes the token's `sub` to no log line, while its single `query.cypher` audit row
    records that `sub` as the actor. Drives the real mounted transport end to end (`initialize`
    -> `notifications/initialized` -> `tools/call`) with a real `Authorization: Bearer <token>`
    header on every request; `execute_cypher_query` itself is never monkeypatched.
    """
    emitter = configure()
    store = InMemoryAuditStore()
    _override(monkeypatch, audit_store_factory=audit_store_factory(store))
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-42"
    token = mock_oidc_provider.mint_token(sub=token_sub)

    _serve_a_seeded_fake_graph(monkeypatch)

    call_response = _call_cypher_tool_over_bearer_http(auth_context=auth_context, token=token)

    assert call_response.status_code == 200
    result_text = _sse_result_text(call_response.text)
    assert json.loads(result_text)["row_count"] == 1

    emitter.flush()
    lines = read_lines(resolve_default_log_path())
    entry = next(
        line
        for line in lines
        if line.get("component") == "query_engine" and line.get("action") == "execute_cypher_query"
    )
    assert "principal" not in entry
    assert all(token_sub not in json.dumps(line) for line in lines)
    assert [(row.action, row.outcome) for row in store.rows] == [("query.cypher", "applied")]
    assert store.rows[0].actor_subject == token_sub


def test_bearer_authenticated_mcp_request_logs_authentication_without_the_token_subject(
    monkeypatch: pytest.MonkeyPatch,
    mock_oidc_provider: MockOidcProvider,
    read_lines: ReadLines,
) -> None:
    """A bearer-authenticated `initialize` -> `tools/call` flow logs each successful
    `auth`/`authenticate_request` entry with component, action and outcome only: neither the
    token's subject nor its issuer reaches the log.
    """
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-42"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    _serve_a_seeded_fake_graph(monkeypatch)

    call_response = _call_cypher_tool_over_bearer_http(auth_context=auth_context, token=token)

    assert call_response.status_code == 200
    emitter.flush()
    lines = read_lines(resolve_default_log_path())
    auth_entries = [
        line
        for line in lines
        if line.get("component") == "auth" and line.get("action") == "authenticate_request"
    ]
    assert auth_entries, "no authentication entry was written"
    for entry in auth_entries:
        assert entry["outcome"] == "success"
        assert set(entry) - {"run_id"} == {"component", "action", "outcome", "timestamp"}
        serialized = json.dumps(entry)
        assert token_sub not in serialized
        assert mock_oidc_provider.issuer not in serialized


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
