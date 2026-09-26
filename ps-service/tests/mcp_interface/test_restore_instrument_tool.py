"""Tests for the registered `restore_instrument` MCP tool (issue #127, Group 2).

Slice 2.1 covers the happy path: `restore_instrument(instrument_id=...)`
(D-INSTRUMENT-ID-STRICTNESS: `instrument_id` validated at the MCP schema
layer against `_RESTORE_INSTRUMENT_ID_PATTERN`, at least as strict as
ps-cli's own `_instrument_id_type` callback) delegates in-process to
`run_restoration_from_catalog_source` (D-RESTORE-DELEGATE -- the exact same
function `POST /restorations/from-catalog`'s own route calls), via the
shared `_run_mcp_action` audit wrapper (D-AUDIT-WRAPPER), returning the
returned `RestorationAcceptedResponse.model_dump()` verbatim -- the same
`instrument_id`/`stages` shape ps-cli's own `restore instrument` used to
print.

Slice 2.2 (added below in a follow-up edit) wires the remaining
D-SANITIZE-RESTORE error-taxonomy rows and the residual unexpected-exception
safety net.

Slice 2.3 (added below in a follow-up edit) proves principal resolution
(both the `mcp_interface` audit log and the delegate's own `actor` kwarg)
under a real verified bearer token.

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_get_catalog_listing_tool.py`'s/`test_restorations_from_catalog.py`'s
own convention. `FakeCuratedArtifactTransport`/`FakeFailingCuratedSourceTransport`
(`tests/api/_fakes.py`) are the existing fixtures the REST-side
`POST /restorations/from-catalog` test suite already uses -- reused here
unchanged, not reinvented, per PLAN.md's own instruction. `_FakeDb`,
`_FakeCatalogRestoreStage`, `_fetch_artifact_through`, and
`_fake_dependencies` mirror `test_restorations_from_catalog.py`'s own
identically-named/-shaped helpers (that file's fixtures are local to it, not
exported, so they are mirrored here rather than imported).

`pytest-asyncio` is not installed; this file drives the tool with a bare
`asyncio.run(server.call_tool(...))`, exactly like
`test_get_catalog_listing_tool.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from typing import TYPE_CHECKING, NoReturn, cast

import pytest
from api._fakes import FakeCuratedArtifactTransport, FakeFailingCuratedSourceTransport
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette

from ps_service.api.restore_orchestration import CatalogRestoreDependencies
from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.curated_source.artifact_client import fetch_artifact
from ps_service.curated_source.resolve import EffectiveCatalogSource
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server
from ps_service.mcp_interface.http_transport import (
    MCP_HTTP_MOUNT_PATH,
    build_streamable_http_app,
)
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreOutcome
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    import urllib.request
    from collections.abc import AsyncGenerator, Callable
    from pathlib import Path

    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.config import ServiceConfig
    from ps_service.curated_source.artifact_client import FetchArtifactCall, FetchedArtifact
    from ps_service.curated_source.http_fetch import CuratedSourceTransport
    from ps_service.restore.models import RestoreArtifact
    from ps_test_support.mock_oidc_provider import MockOidcProvider

    type ReadLines = Callable[[Path], list[dict[str, object]]]

_BASE_URL = "http://127.0.0.1:8000"
_JSON_RPC_ACCEPT = "application/json, text/event-stream"
_ALLOWED_ALGORITHMS = frozenset({"RS256"})

_INSTRUMENT_ID = "CRA-1.0"

_VALID_MANIFEST: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "celex": "32024R2847",
    "title": "Cyber Resilience Act",
    "short_name": "CRA",
    "version": "1.0",
    "source_type": "external",
    "jurisdiction": "EU",
    "schema_version": "1",
    "exported_at": "2026-01-01T00:00:00Z",
    "baseline_sha256": "a" * 64,
    "native_sha256": "b" * 64,
}
_BASELINE_BYTES = b'{"nodes": [], "edges": []}'
_NATIVE_BYTES = b'{"nodes": [], "edges": []}'


def _valid_transport() -> FakeCuratedArtifactTransport:
    return FakeCuratedArtifactTransport(
        {
            "manifest.json": json.dumps(_VALID_MANIFEST).encode("utf-8"),
            "baseline.json": _BASELINE_BYTES,
            "native.json": _NATIVE_BYTES,
        }
    )


@dataclasses.dataclass
class _FakeDb:
    """A stand-in for `falkordb.FalkorDB` -- never actually touched by these fakes."""


class _FakeCatalogRestoreStage:
    """Records every call (including the `actor` kwarg) and returns/raises a result."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._error = error

    def __call__(
        self,
        artifact: RestoreArtifact,
        *,
        db: object,
        single_tenant_graph_name: str,
        similarity_threshold: float,
        actor: str,
        emitter: object | None = None,
        source: str | None = None,
    ) -> RestoreOutcome:
        _ = (db, emitter)
        self.calls.append(
            {
                "single_tenant_graph_name": single_tenant_graph_name,
                "similarity_threshold": similarity_threshold,
                "actor": actor,
                "source": source,
            }
        )
        if self._error is not None:
            raise self._error
        return RestoreOutcome(
            instrument_id=artifact.manifest.instrument_id,
            stages=("verified", "staged", "merged_and_finalized"),
        )


def _fetch_artifact_through(transport: CuratedSourceTransport) -> FetchArtifactCall:
    def _call(base_url: str, instrument_id: str) -> FetchedArtifact:
        return fetch_artifact(base_url, instrument_id, transport=transport)

    return _call


def _fake_dependencies(
    transport: CuratedSourceTransport, stage: _FakeCatalogRestoreStage
) -> CatalogRestoreDependencies:
    def _resolve_effective_source(config: ServiceConfig) -> EffectiveCatalogSource:
        return EffectiveCatalogSource(url=config.curated_source_base_url, is_override=False)

    return CatalogRestoreDependencies(
        fetch_artifact=_fetch_artifact_through(transport),
        resolve_effective_source=_resolve_effective_source,
        open_db=lambda config: cast("FalkorDB", _FakeDb()),
        single_tenant_graph_name=lambda config: "policy_system",
        restore=stage,
    )


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _call_restore_instrument(instrument_id: str = _INSTRUMENT_ID) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool("restore_instrument", {"instrument_id": instrument_id})
    )
    assert isinstance(result, CallToolResult)
    return result


def _set_similarity_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.83")


# --- Slice 2.1: happy path ----------------------------------------------------


def test_happy_path_returns_accepted_response_shape_and_logs_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-002/004: the returned `instrument_id`/`stages` match
    `RestorationAcceptedResponse`'s own field-for-field shape -- the same
    summary ps-cli's `restore instrument` used to print. Also asserts the
    `mcp_interface` started/succeeded log pair carries the resolved
    principal (AC-BI-007, restore half -- partial; Slice 2.3 proves the
    failed-call case and the real-bearer-token case).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    stage = _FakeCatalogRestoreStage()
    transport = _valid_transport()
    fake_dependencies = _fake_dependencies(transport, stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["instrument_id"] == _INSTRUMENT_ID
    assert [s["stage"] for s in body["stages"]] == [
        "verified",
        "staged",
        "merged_and_finalized",
    ]

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "restore_instrument"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


def test_happy_path_fetches_the_artifact_from_the_curated_source_not_a_local_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002/004: the fake `fetch_artifact` HTTP transport was hit for exactly
    `manifest.json`/`baseline.json`/`native.json` under the instrument's own
    path -- proving the fetch, not a local `catalog_repo` read, supplied the
    artifact (no local-machine dependency).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    stage = _FakeCatalogRestoreStage()
    transport = _valid_transport()
    fake_dependencies = _fake_dependencies(transport, stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    requested_filenames = {req.full_url.rsplit("/", 1)[-1] for req in transport.requests}
    assert requested_filenames == {"manifest.json", "baseline.json", "native.json"}
    for req in transport.requests:
        assert f"/{_INSTRUMENT_ID}/" in req.full_url


def test_happy_path_calls_the_restore_delegate_with_the_resolved_principal_as_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake `restore` delegate is called with `actor` equal to the
    resolved principal (D-RESTORE-DELEGATE's `actor=principal or "unknown"`
    convention).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    stage = _FakeCatalogRestoreStage()
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    assert len(stage.calls) == 1
    assert stage.calls[0]["actor"] == LOCAL_TEST_PRINCIPAL_ID


# --- Slice 2.1: validation (D-INSTRUMENT-ID-STRICTNESS) ------------------------


def test_malformed_instrument_id_is_rejected_at_the_schema_layer_before_the_body_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-INSTRUMENT-ID-STRICTNESS: the combined `Field(pattern=...)` +
    `AfterValidator(_reject_path_traversal_segment)` schema (see
    `mcp_server.py`'s own comment on why this is two validators, not one
    regex: pydantic-core's regex backend has no look-around support) rejects
    a leading hyphen, a forward slash, and two `".."`-containing values --
    the same cases `test_parser.py`'s own `_instrument_id_type` tests cover
    -- before `restore_instrument`'s body ever runs (the fake delegate is
    never called). A bare in-process `server.call_tool` (this module's own
    convention throughout) propagates that schema rejection as a raised
    `ToolError`, not a returned `CallToolResult(is_error=True)` -- confirmed
    by reading `MCPServer.call_tool`'s own body, which skips the
    `_handle_call_tool` wire-level handler's `except Exception -> CallToolResult`
    translation that a real transport call goes through.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    stage = _FakeCatalogRestoreStage()

    class _NeverCalledTransport:
        def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
            _ = (request, timeout)
            message = "must not be called for a schema-rejected call"
            raise AssertionError(message)

    fake_dependencies = _fake_dependencies(_NeverCalledTransport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    for bad_instrument_id in ("-leading-hyphen", "has/slash", "../etc/passwd", "a/../b"):
        with pytest.raises(ToolError):
            _call_restore_instrument(bad_instrument_id)
        assert stage.calls == []


# --- Slice 2.2 auth infra: mirrors test_get_catalog_listing_tool.py's/
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

    Mirrors `test_get_catalog_listing_tool.py`'s/`test_ingest_regulation_tool
    .py`'s own `_authenticated_test_client` exactly -- this module's Slice
    2.3 tests drive `restore_instrument` (not `cypher`) through the same
    genuinely auth-enforcing mounted app, never the bypass-shaped one every
    other test in this module builds via a bare `server.call_tool`.
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


def _call_restore_instrument_over_http(client: TestClient, *, token: str) -> str:
    """Drive `restore_instrument` through the real mounted transport with a
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
                    "name": "test-restore-instrument-auth-client",
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
            "params": {
                "name": "restore_instrument",
                "arguments": {"instrument_id": _INSTRUMENT_ID},
            },
        },
        headers={
            "Accept": _JSON_RPC_ACCEPT,
            "Authorization": f"Bearer {token}",
            "mcp-session-id": session_id,
        },
    )
    assert call_response.status_code == 200
    return _sse_result_text(call_response.text)


def test_real_verified_token_principal_threads_through_to_audit_log_and_delegate_actor(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """AC-BI-001/007: under a real, verified bearer token -- driven through
    the actual mounted Streamable HTTP transport, mirroring
    `test_get_catalog_listing_tool.py`'s own pattern, never a bare in-process
    `server.call_tool` -- the token's `sub` claim (never
    `LOCAL_TEST_PRINCIPAL_ID`) reaches both the `mcp_interface` log entries'
    `principal` AND `run_restoration_from_catalog_source`'s own delegate
    `actor` kwarg.
    """
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-restore-42"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    stage = _FakeCatalogRestoreStage()
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_restore_instrument_over_http(client, token=token)

    body = json.loads(result_text)
    assert body["instrument_id"] == _INSTRUMENT_ID
    assert len(stage.calls) == 1
    assert stage.calls[0]["actor"] == token_sub

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "restore_instrument"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)
    assert not any(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)


def test_local_test_bypass_principal_still_threads_through_when_no_token_is_presented(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """AC-BI-001: `_resolve_principal`'s existing, unchanged fallback
    contract -- with no bearer token presented at all (a bare in-process
    `server.call_tool`) and the local-test bypass active,
    `LOCAL_TEST_PRINCIPAL_ID` still reaches the `mcp_interface` log entries'
    `principal` (already proven by Slice 2.1's own test; re-proven here as
    this slice's own dedicated auth/audit completeness check, mirroring
    `test_get_catalog_listing_tool.py`'s identically-purposed test).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    stage = _FakeCatalogRestoreStage()
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "restore_instrument"
    ]
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)


def test_failed_call_under_a_real_verified_token_still_carries_the_subs_principal(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """AC-BI-007: a FAILED call -- reusing Slice 2.2's checksum-rejection
    branch -- still carries the resolved real-token principal on its
    `outcome="failed"` `mcp_interface` log entry, not only the succeeded-call
    case proven above.
    """
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-restore-failed-7"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    stage = _FakeCatalogRestoreStage(error=ArtifactIntegrityError("checksum mismatch"))
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_restore_instrument_over_http(client, token=token)

    assert result_text.startswith("error: ")

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "restore_instrument"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "failed"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)


# --- Slice 2.2: failure-state completeness -------------------------------------


def test_curated_source_unreachable_returns_named_error_and_never_calls_the_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-RESTORE: an unreachable curated-content source
    (`CuratedSourceUnavailableError`, raised by `run_restoration_from_catalog_source`
    when `fetch_artifact` raises `CuratedSourceFetchError`) is caught and
    returned as `error: <str(exc)>` verbatim -- the restore delegate is never
    called.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    stage = _FakeCatalogRestoreStage()
    fake_dependencies = _fake_dependencies(FakeFailingCuratedSourceTransport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert text != "error: an unexpected error occurred"
    assert stage.calls == []


def test_checksum_mismatch_returns_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-SANITIZE-RESTORE: the fetched artifact's checksum doesn't match its
    own manifest (`ArtifactIntegrityError` from the fake `restore` delegate)
    -> `RestoreArtifactRejectedError`, returned as `error: <str(exc)>`.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    stage = _FakeCatalogRestoreStage(error=ArtifactIntegrityError("checksum mismatch"))
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "checksum mismatch" in text


def test_schema_version_mismatch_returns_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-SANITIZE-RESTORE: `ArtifactSchemaVersionMismatchError` from the fake
    `restore` delegate -> `RestoreArtifactRejectedError`, returned verbatim.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    stage = _FakeCatalogRestoreStage(error=ArtifactSchemaVersionMismatchError("schema mismatch"))
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "schema mismatch" in text


def test_restore_stage_failure_returns_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-SANITIZE-RESTORE: any other restore-stage failure (a generic
    exception from the fake `restore` delegate) -> `RestoreStageFailedError`,
    returned as `error: <str(exc)>` naming the failing stage.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    stage = _FakeCatalogRestoreStage(error=RuntimeError("disk full"))
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert text != "error: an unexpected error occurred"


def test_missing_similarity_threshold_returns_named_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-RESTORE: `_require_similarity_threshold` raises
    `RestoreStageFailedError(stage="configuration", ...)` directly, outside
    any try/except inside `run_restoration_from_catalog_source`, when
    `PS_COMPANYMERGE_SIMILARITY_THRESHOLD` is unset -- reaches the tool the
    same uncaught way, and is still caught by this tool's own
    `RestoreStageFailedError` handler.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    monkeypatch.delenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", raising=False)
    configure()
    stage = _FakeCatalogRestoreStage()
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: fake_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    text = _text(result)
    assert text.startswith("error: ")
    assert "PS_COMPANYMERGE_SIMILARITY_THRESHOLD" in text
    assert stage.calls == []


def test_graph_unavailable_returns_generic_graph_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-SANITIZE-RESTORE: `dependencies.open_db` raising any exception is
    wrapped by `_sanitize_restore_graph_opens` into `McpGraphUnavailableError`,
    surfaced as the shared, fixed `_GRAPH_UNAVAILABLE_MESSAGE` -- never the
    raw driver exception.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    configure()
    stage = _FakeCatalogRestoreStage()
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)

    def _raising_open_db(config: ServiceConfig) -> FalkorDB:
        _ = config
        message = "connection refused -- must never reach the caller"
        raise ConnectionRefusedError(message)

    broken_dependencies = dataclasses.replace(fake_dependencies, open_db=_raising_open_db)
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: broken_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"
    assert stage.calls == []


def test_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4: an exception the tool body does not itself
    sanitise (here, `dependencies.fetch_artifact` raising something
    unclassified, not `CuratedSourceFetchError`) is caught by
    `_run_mcp_action`'s residual safety net -- returned as the fixed,
    generic message (never the raw exception text), with the full `repr`
    logged server-side only.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    _set_similarity_threshold(monkeypatch)
    emitter = configure()
    stage = _FakeCatalogRestoreStage()
    fake_dependencies = _fake_dependencies(_valid_transport(), stage)

    def _raising_fetch_artifact(base_url: str, instrument_id: str) -> FetchedArtifact:
        _ = (base_url, instrument_id)
        message = "boom -- must never reach the caller"
        raise ValueError(message)

    broken_dependencies = dataclasses.replace(
        fake_dependencies, fetch_artifact=_raising_fetch_artifact
    )
    monkeypatch.setattr(
        mcp_server, "build_default_restore_from_catalog_dependencies", lambda: broken_dependencies
    )

    result = _call_restore_instrument()

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "restore_instrument"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert failed_line.get("principal") == LOCAL_TEST_PRINCIPAL_ID
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))
    assert "ValueError" in str(failed_line.get("detail"))
