"""Tests for the registered `get-catalog-listing` MCP tool (issue #127, Group 1).

Slice 1.1 covers the happy path: `get_catalog_listing` (zero parameters --
AC-BI-005's validation surface does not apply, mirrors `check_regulations`/
`near_misses_list`) delegates in-process to `resolve_effective_source` +
`fetch_catalog` (D-CATALOG-NO-ORCH-LAYER, a byte-for-byte mirror of
`list_curated_catalog`'s own body, `routes.py:224-240`), via the shared
`_run_mcp_action` audit wrapper (D-AUDIT-WRAPPER), returning a
`CuratedCatalogResponse.model_dump()`.

Slice 1.2 (added below in a follow-up edit) wires `CuratedSourceFetchError`
(D-CATALOG-ERROR) and the residual unexpected-exception safety net.

Slice 1.3 (added below in a follow-up edit) proves principal resolution
under a real verified bearer token.

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_catalog_source_skills.py`'s/`test_check_regulations_tool.py`'s own
convention. The fake `CuratedCatalogDependencies` bundle
(`tests/api/_fakes.py::build_fake_curated_catalog_dependencies`,
`FakeCuratedSourceTransport`) is the existing fixture the REST-side
`GET /catalog` test suite already uses -- reused here unchanged, not
reinvented, per PLAN.md's own instruction.

`pytest-asyncio` is not installed; this file drives the tool with a bare
`asyncio.run(server.call_tool(...))`, exactly like `test_cypher_tool.py`/
`test_catalog_source_skills.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from typing import TYPE_CHECKING, cast

from api._fakes import (
    FakeCuratedSourceTransport,
    FakeFailingCuratedSourceTransport,
    build_fake_curated_catalog_dependencies,
)
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette

from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
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

    from ps_test_support.mock_oidc_provider import MockOidcProvider

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_BASE_URL = "http://127.0.0.1:8000"
_JSON_RPC_ACCEPT = "application/json, text/event-stream"
_ALLOWED_ALGORITHMS = frozenset({"RS256"})


_CANNED_ENTRIES = [
    {
        "instrument_id": "CRA-1.0",
        "celex": "32024R2847",
        "title": "Cyber Resilience Act",
        "source_type": "external",
        "jurisdiction": "EU",
        "short_name": "CRA",
        "version": "1.0",
    },
    {
        "instrument_id": "internal-policy-1",
        "celex": None,
        "title": "Internal Data Handling Policy",
        "source_type": "internal",
        "jurisdiction": None,
        "short_name": "idhp",
        "version": "1.0",
    },
]


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _call_get_catalog_listing() -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("get-catalog-listing", {}))
    assert isinstance(result, CallToolResult)
    return result


# --- Slice 1.1: happy path ---------------------------------------------------


def test_happy_path_returns_full_listing_with_principal_logged(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-002/003: the full curated catalog is returned field-for-field,
    including an internal entry with `jurisdiction=None`, with no local
    filesystem path read anywhere in the call chain -- the fake HTTP
    transport is the only I/O seam exercised (it takes no `Path` argument at
    all, so no `ps-cli`-style local catalog repo read is possible in this
    path). Also asserts the `mcp_interface` started/succeeded log pair
    carries the resolved principal (AC-BI-007, catalog half -- partial;
    Slice 1.3 proves the failed-call case and the real-bearer-token case).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))
    fake_dependencies = build_fake_curated_catalog_dependencies(transport)
    monkeypatch.setattr(
        mcp_server, "build_default_curated_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_get_catalog_listing()

    assert result.is_error is False
    body = json.loads(_text(result))
    instruments = {entry["instrument_id"]: entry for entry in body["instruments"]}
    assert set(instruments) == {"CRA-1.0", "internal-policy-1"}
    assert instruments["CRA-1.0"] == {
        "instrument_id": "CRA-1.0",
        "title": "Cyber Resilience Act",
        "source_type": "external",
        "jurisdiction": "EU",
    }
    assert instruments["internal-policy-1"] == {
        "instrument_id": "internal-policy-1",
        "title": "Internal Data Handling Policy",
        "source_type": "internal",
        "jurisdiction": None,
    }

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "get_catalog_listing"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


# --- Slice 1.2: failure-state completeness -----------------------------------


def test_source_unreachable_returns_named_error_naming_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-CATALOG-ERROR: `CuratedSourceFetchError` (an unreachable/malformed
    source) is caught directly and returned as `error: <str(exc)>` verbatim
    -- the message already names the source URL and the specific failure, so
    no further sanitisation is applied. Distinct from the generic residual
    safety-net message below (never collapsed into it).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    transport = FakeFailingCuratedSourceTransport()
    fake_dependencies = build_fake_curated_catalog_dependencies(transport)
    monkeypatch.setattr(
        mcp_server, "build_default_curated_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_get_catalog_listing()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert text != "error: an unexpected error occurred"
    assert "catalog.json" in text


def test_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4: an exception the tool body does not itself
    sanitise (here, `dependencies.fetch_catalog` raising something
    unclassified) is caught by `_run_mcp_action`'s residual safety net --
    returned as the fixed, generic message (never the raw exception text),
    with the full `repr` logged server-side only.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    def _raising_fetch_catalog(base_url: str) -> tuple[object, ...]:
        _ = base_url
        message = "boom -- must never reach the caller"
        raise ValueError(message)

    transport = FakeCuratedSourceTransport(b"[]")
    fake_dependencies = build_fake_curated_catalog_dependencies(transport)
    broken_dependencies = dataclasses.replace(
        fake_dependencies, fetch_catalog=_raising_fetch_catalog
    )
    monkeypatch.setattr(
        mcp_server, "build_default_curated_catalog_dependencies", lambda: broken_dependencies
    )

    result = _call_get_catalog_listing()

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "get_catalog_listing"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert failed_line.get("principal") == LOCAL_TEST_PRINCIPAL_ID
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))
    assert "ValueError" in str(failed_line.get("detail"))


# --- Slice 1.3 auth infra: mirrors test_mcp_auth.py's/
# test_ingest_regulation_tool.py's own pattern exactly ------------------------


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

    Mirrors `test_mcp_auth.py::_authenticated_test_client`/
    `test_ingest_regulation_tool.py::_authenticated_test_client` exactly --
    this module's Slice 1.3 tests drive `get-catalog-listing` (not `cypher`)
    through the same genuinely auth-enforcing mounted app, never the
    bypass-shaped one every other test in this module builds via a bare
    `server.call_tool`.
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


def _call_get_catalog_listing_over_http(client: TestClient, *, token: str) -> str:
    """Drive `get-catalog-listing` through the real mounted transport with a
    bearer token: `initialize` -> `notifications/initialized` -> `tools/call`,
    the same JSON-RPC sequence `test_mcp_auth.py` already establishes.
    """
    init_response = client.post(
        f"{MCP_HTTP_MOUNT_PATH}/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-get-catalog-listing-auth-client",
                    "version": "0.0.1",
                },
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
            "params": {"name": "get-catalog-listing", "arguments": {}},
        },
        headers={
            "Accept": _JSON_RPC_ACCEPT,
            "Authorization": f"Bearer {token}",
            "mcp-session-id": session_id,
        },
    )
    assert call_response.status_code == 200
    return _sse_result_text(call_response.text)


def test_real_verified_token_principal_threads_through_to_audit_log(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """AC-BI-001/007: under a real, verified bearer token -- driven through
    the actual mounted Streamable HTTP transport, mirroring
    `test_mcp_auth.py`'s own pattern, never a bare in-process
    `server.call_tool` -- the token's `sub` claim (never
    `LOCAL_TEST_PRINCIPAL_ID`) reaches the `mcp_interface` log entries'
    `principal`. Unlike `ingest_regulation`, `get_catalog_listing`'s own
    delegate (`fetch_catalog`) takes no `caller`/principal parameter of its
    own, so there is no second, downstream log entry to assert on here.
    """
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-catalog-42"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))
    fake_dependencies = build_fake_curated_catalog_dependencies(transport)
    monkeypatch.setattr(
        mcp_server, "build_default_curated_catalog_dependencies", lambda: fake_dependencies
    )

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_get_catalog_listing_over_http(client, token=token)

    body = json.loads(result_text)
    assert {entry["instrument_id"] for entry in body["instruments"]} == {
        "CRA-1.0",
        "internal-policy-1",
    }

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "get_catalog_listing"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)
    assert not any(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)


def test_local_test_bypass_principal_still_threads_through_when_no_token_is_presented(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-001: `_resolve_principal`'s existing, unchanged fallback
    contract -- with no bearer token presented at all (a bare in-process
    `server.call_tool`, like every Slice 1.1/1.2 test above) and the
    local-test bypass active, `LOCAL_TEST_PRINCIPAL_ID` still reaches the
    `mcp_interface` log entries' `principal` (already proven by Slice 1.1's
    own test; re-proven here as this slice's own dedicated auth/audit
    completeness check, mirroring `test_ingest_regulation_tool.py`'s
    identically-purposed test).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    transport = FakeCuratedSourceTransport(json.dumps(_CANNED_ENTRIES).encode("utf-8"))
    fake_dependencies = build_fake_curated_catalog_dependencies(transport)
    monkeypatch.setattr(
        mcp_server, "build_default_curated_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_get_catalog_listing()

    assert result.is_error is False
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "get_catalog_listing"
    ]
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)


def test_failed_call_under_a_real_verified_token_still_carries_the_subs_principal(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """AC-BI-007: a FAILED call -- reusing Slice 1.2's source-unreachable
    `CuratedSourceFetchError` branch -- still carries the resolved
    real-token principal on its `outcome="failed"` `mcp_interface` log
    entry, not only the succeeded-call case proven above.
    """
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-catalog-failed-7"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    transport = FakeFailingCuratedSourceTransport()
    fake_dependencies = build_fake_curated_catalog_dependencies(transport)
    monkeypatch.setattr(
        mcp_server, "build_default_curated_catalog_dependencies", lambda: fake_dependencies
    )

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_get_catalog_listing_over_http(client, token=token)

    assert result_text.startswith("error: ")
    assert "catalog.json" in result_text

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "get_catalog_listing"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "failed"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)
