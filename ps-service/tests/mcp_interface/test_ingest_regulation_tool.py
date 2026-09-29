"""Tests for the registered `ingest_regulation` MCP tool (issue #126, Slices 1.1-1.3).

Slice 1.1 covers the curated-CELEX happy path: `find_by_celex` resolves a real
catalog entry, the caller-supplied `short_name` matches the catalog's own
value, and the tool delegates in-process to `run_catalog_ingestion_pipeline`
(D-DELEGATE) via the new `_run_mcp_action` audit wrapper (D-AUDIT-WRAPPER),
returning `routes._to_accepted_response(...).model_dump()` (D-RESPONSE-SHAPE).

Slice 1.2 covers the non-curated (Cellar-fallback) path -- the actual #96 fix
(CHANGES.md C1/Appendix C1): when `find_by_celex` returns `None`, the tool
calls `resolve_via_cellar(celex, short_name=short_name)` with the
caller-supplied, already-pattern-validated `short_name`, which is used
verbatim -- `_derive_short_name` is never reached on this path, so two
resolutions of the same CELEX with differently-worded Cellar titles no
longer diverge into two different `short_name`s/graph names.

Slice 1.3 wires the remaining D-SANITIZE-UNEXPECTED error-taxonomy rows for
this tool: `IngestionConfigIncompleteError`, `PipelineStageError`, the
D-PREFLIGHT LLM-Interface-unhealthy check, the graph-unavailable sanitiser
around each of `run_catalog_ingestion_pipeline`'s three graph-open call
sites, and `_run_mcp_action`'s own residual unexpected-exception safety net
(D-AUDIT-WRAPPER point 4), proven here specifically for `ingest_regulation`.

Slice 1.4 proves D-AUTH's auth/audit-completeness claim end to end under a
*real, verified* bearer token -- not just the local-test-bypass principal
every test above exercises via a bare in-process `server.call_tool`. It
drives the actual mounted Streamable HTTP transport against the shared
`MockOidcProvider` test infra, mirroring `test_mcp_auth.py`'s own
`_authenticated_test_client`/JSON-RPC-sequence pattern (reused/mirrored, not
reinvented), proving: (a) a verified token's `sub` reaches both
`_run_mcp_action`'s `mcp_interface` log entries (`principal`) and
`run_catalog_ingestion_pipeline`'s own `ingestion_run` log entries
(`caller`); (b) the local-test-bypass fallback still threads
`LOCAL_TEST_PRINCIPAL_ID` through both of those same log entries when no
token is presented; (c) a **failed** call under a real token still carries
that token's principal on its `outcome="failed"` log entry.

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_cypher_tool.py`'s own convention. The fake `PipelineDependencies`
bundle (`GraphOpeners`/`PipelineStages`/`PipelineAdapters`) is the existing
`tests/api/_fakes.py::build_fake_pipeline_dependencies` fixture already used
by the REST-side `run_catalog_ingestion_pipeline` tests
(`tests/api/test_ingestion_orchestration.py`) -- reused here unchanged, not
reinvented, per PLAN.md's own instruction.

`pytest-asyncio` is not installed; most of this file's tests drive the tool
with bare `asyncio.run(server.call_tool(...))`, exactly like
`test_cypher_tool.py`. Slice 1.4's real-token tests instead drive the real
mounted ASGI transport through `TestClient`, since a bare `server.call_tool`
never populates the `get_access_token()` contextvar a real token requires.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from typing import TYPE_CHECKING, cast

from api._fakes import build_fake_pipeline_dependencies
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- issue #145: same cross-package import `test_catalog_source_authz_gate.py` already establishes, reused here so this file's own two real-token tests can grant the caller `ComplianceOfficer` on the new gate
    FakeAccessRoleStore,
)
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette

from ps_service import dependency_health
from ps_service.api.catalog import CatalogEntry, find_by_celex
from ps_service.auth.models import AuthContext
from ps_service.auth.verifier import PsTokenVerifier
from ps_service.authz.models import AccessRole
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.ingestion.adapters.errors import CellarNotFoundError
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


def _curated_cra_entry() -> CatalogEntry:
    """A real curated catalog entry, so this test never hand-duplicates the
    catalog's own celex/short_name/version values.
    """
    entry = find_by_celex("32024R2847")
    assert entry is not None, "fixture assumption: CRA is a curated catalog entry"
    return entry


_CURATED_ENTRY = _curated_cra_entry()
_CELEX = _CURATED_ENTRY.celex
_SHORT_NAME = _CURATED_ENTRY.short_name
_VERSION = _CURATED_ENTRY.version

# --- Slice 1.2 fixtures: a CELEX absent from the curated catalog, resolved via
# Cellar/ELI -- mirrors tests/api/test_ingestion_orchestration.py's own
# `_NONCURATED_CELEX`/Fixture-A/RDF-fixture shapes, not reinvented.
_NONCURATED_CELEX = "32020R1111"

_RDF_FIXTURE_REGULATION_A = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32020R1111">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32020R1111</j.0:resource_legal_id_celex>
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2030-01-01</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""


def _xhtml_fixture(title: str) -> bytes:
    """A Regulation-shaped Cellar XHTML fixture carrying `title` as its main title."""
    return f"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">{title}</div>
<div class="eli-subdivision" id="cpt_I">
<div class="eli-title" id="cpt_I.tit_1">CHAPTER I General provisions</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Entry into force and application</div>
<div>This Regulation shall enter into force on the twentieth day following
publication. It shall apply from 1 January 2030.</div>
</div>
</div>
</div>
</body>
</html>
""".encode()


def _configure_complete_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the three config fields `_require_ingestion_config` needs to be non-`None`."""
    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", "azure/gpt-4o")
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", "azure/text-embedding-3-large")
    monkeypatch.setenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", "0.83")


def _call_ingest_regulation(celex: str, short_name: str) -> CallToolResult:
    result = asyncio.run(
        mcp_server.server.call_tool("ingest_regulation", {"celex": celex, "short_name": short_name})
    )
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# --- Slice 1.4 auth infra: mirrors test_mcp_auth.py's own pattern exactly --


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

    Mirrors `test_mcp_auth.py::_authenticated_test_client` exactly -- this
    module's Slice 1.4 tests drive `ingest_regulation` (not `cypher`) through
    the same genuinely auth-enforcing mounted app, never the bypass-shaped
    one every other test in this module builds via a bare
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


def _call_ingest_regulation_over_http(
    client: TestClient, *, token: str, celex: str, short_name: str
) -> str:
    """Drive `ingest_regulation` through the real mounted transport with a
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
                "clientInfo": {"name": "test-ingest-regulation-auth-client", "version": "0.0.1"},
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
                "name": "ingest_regulation",
                "arguments": {"celex": celex, "short_name": short_name},
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


def test_curated_celex_happy_path_returns_ingestion_accepted_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-RESPONSE-SHAPE: the returned dict matches `IngestionAcceptedResponse`
    field for field -- run_id, regulatory_instrument_id, source, outcome, stages.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["run_id"]
    assert isinstance(body["run_id"], str)
    assert body["regulatory_instrument_id"] == "cra-1.0"
    assert body["source"] == "catalog"
    assert [stage["stage"] for stage in body["stages"]] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]
    assert all(stage["status"] == "succeeded" for stage in body["stages"])
    assert body["outcome"] == "fresh"
    assert set(body.keys()) == {
        "run_id",
        "regulatory_instrument_id",
        "source",
        "outcome",
        "stages",
    }
    for stage in body["stages"]:
        assert set(stage.keys()) == {"stage", "status", "summary"}


def test_curated_celex_happy_path_calls_each_stage_exactly_once_with_the_curated_entrys_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake ingest/extract/derive/merge stage functions are each called
    exactly once, and the ingest stage receives the curated entry's own
    celex/short_name/version fields (D-DELEGATE: real orchestration, not a
    reimplementation).
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]
    ingest_call = fake.recorder.calls[0]
    assert ingest_call.kwargs["identifier"] == _CELEX
    assert ingest_call.kwargs["short_name"] == _SHORT_NAME
    assert ingest_call.kwargs["version"] == _VERSION
    downstream = [call for call in fake.recorder.calls if call.stage != "ingestion"]
    assert all(call.regulatory_instrument_id == "cra-1.0" for call in downstream)


def test_curated_celex_happy_path_emits_one_started_succeeded_log_pair_with_principal(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER: `_run_mcp_action` emits exactly one `mcp_interface`
    started/succeeded log-line pair, carrying the resolved principal (here,
    the local-test-bypass principal, since no bearer token is presented to
    a bare `server.call_tool` in-process invocation).
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in lines)
    assert len({line["run_id"] for line in lines}) == 1
    for line in lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


def test_curated_celex_mismatched_short_name_is_rejected_before_the_pipeline_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SHORTNAME-CURATED-MISMATCH: a caller-supplied `short_name` that does
    not match the curated entry's own value is rejected with the named error
    string, and no stage ever runs.

    Issue #145: this test predates the tool's authz gate and never set the
    local-test bypass (harmless before the gate existed, since nothing else
    in the un-gated body needed it) -- now required, like every sibling
    body-logic test in this file, so the call reaches D-SHORTNAME-CURATED-MISMATCH
    at all rather than being denied first by the new gate.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_CELEX, "not-the-real-short-name")

    assert result.is_error is False
    assert _text(result) == (
        f"error: CELEX {_CELEX} is curated under short_name '{_SHORT_NAME}'; "
        "pass that value, not 'not-the-real-short-name'"
    )
    assert fake.recorder.calls == []


def test_short_name_collision_with_an_already_ingested_different_celex_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-006: a non-curated CELEX whose ``short_name`` is already
    recorded in the single-tenant graph under a *different* CELEX is rejected with
    the named error string, before any pipeline stage runs -- MCP parity with
    ``POST /ingestions``'s own graph-side collision check.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    given_short_name = "eu-1111-2020"
    fake = build_fake_pipeline_dependencies(collision_row=(f"{given_short_name}-1.0", "32024R0001"))
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_NONCURATED_CELEX, given_short_name)

    assert result.is_error is False
    assert _text(result) == (
        f"error: short_name '{given_short_name}' is already claimed by CELEX 32024R0001"
    )
    assert fake.recorder.calls == []


def test_short_name_collision_with_a_curated_catalog_entry_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-006: a curated CELEX whose own ``short_name`` is already
    claimed by a *different* curated entry is rejected with the named error string,
    before any pipeline stage runs -- the catalog-side collision check.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fixture = (
        CatalogEntry("32024R0001", "Fixture One", "shared-name", "1.0"),
        CatalogEntry("32024R0002", "Fixture Two", "shared-name", "1.0"),
    )
    monkeypatch.setattr("ps_service.api.catalog.REGULATION_CATALOG", fixture)
    fake = build_fake_pipeline_dependencies()
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation("32024R0001", "shared-name")

    assert result.is_error is False
    assert _text(result) == (
        "error: short_name 'shared-name' is already claimed by CELEX 32024R0002"
    )
    assert fake.recorder.calls == []


def test_non_curated_celex_repeated_ingestion_with_drifting_titles_resolves_identical_short_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for issue #96 (CHANGES.md C1/Appendix C1).

    Two sequential `ingest_regulation` calls for the same non-curated CELEX,
    against a fake Cellar/ELI adapter that returns a *differently-worded*
    title on each call (simulating a re-crawl rendering drift), must both
    resolve to the identical, caller-supplied `short_name` -- the same value
    used to construct the `{short_name}_native`/`{short_name}_baseline` graph
    keys (`ingestion/falkordb_client.py`). Before this fix, `resolve_via_cellar`
    derived `short_name` by slugging the fetched title internally, so these
    two calls would have diverged into two different graph names for the
    same CELEX -- the exact "forks a duplicate RegulatoryInstrument subtree"
    defect #96 describes. Now, `_derive_short_name` is never reached on this
    path at all.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="1111-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    titles = iter(
        [
            "Regulation (EU) 1111/1111 First Crawl Title",
            "Regulation (EU) 1111/1111 Reworded On A Later Crawl",
        ]
    )

    def _fetch_xhtml(celex: str) -> bytes:
        _ = celex
        return _xhtml_fixture(next(titles))

    def _fetch_rdf(celex: str) -> bytes:
        _ = celex
        return _RDF_FIXTURE_REGULATION_A

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _fetch_xhtml)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_rdf", _fetch_rdf)

    given_short_name = "eu-1111-2020"
    first = _call_ingest_regulation(_NONCURATED_CELEX, given_short_name)
    second = _call_ingest_regulation(_NONCURATED_CELEX, given_short_name)

    assert first.is_error is False
    assert second.is_error is False
    ingest_calls = [call for call in fake.recorder.calls if call.stage == "ingestion"]
    assert len(ingest_calls) == 2
    assert ingest_calls[0].kwargs["short_name"] == given_short_name
    assert ingest_calls[1].kwargs["short_name"] == given_short_name
    first_body = json.loads(_text(first))
    second_body = json.loads(_text(second))
    assert first_body["regulatory_instrument_id"] == second_body["regulatory_instrument_id"]


def test_non_curated_celex_not_found_on_cellar_returns_catalog_identifier_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: a CELEX absent from both the curated catalog and
    Cellar/ELI returns `error: <str(exc)>` verbatim, and no stage ever runs.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies()
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    def _not_found_fetch(celex: str) -> bytes:
        raise CellarNotFoundError(f"CELEX {celex!r} was not found on Cellar/ELI")

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _not_found_fetch)

    result = _call_ingest_regulation(_NONCURATED_CELEX, "some-short-name")

    assert result.is_error is False
    expected = (
        f"No curated regulation has CELEX {_NONCURATED_CELEX!r}, "
        "and it does not exist on Cellar/ELI."
    )
    assert _text(result) == f"error: {expected}"
    assert fake.recorder.calls == []


# --- Slice 1.3: remaining D-SANITIZE-UNEXPECTED rows for this tool ----------


def test_ingestion_config_incomplete_returns_named_error_before_any_stage_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `IngestionConfigIncompleteError` (raised by
    `_require_ingestion_config`, before any graph or stage call) surfaces as
    `error: <str(exc)>` verbatim, and no stage ever runs.
    """
    monkeypatch.delenv("PS_LLMINTERFACE_MODEL", raising=False)
    monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", raising=False)
    monkeypatch.delenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", raising=False)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == (
        "error: ingestion configuration incomplete: llm_interface_model, "
        "llm_interface_embed_model, company_merge_similarity_threshold not set"
    )
    assert fake.recorder.calls == []


def test_pipeline_stage_error_surfaces_verbatim_with_sanitised_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: a genuine mid-pipeline stage failure surfaces as
    `error: <str(exc)>` verbatim -- already sanitised at the source
    (`_classify_stage_failure`): a non-`is_safe_verbatim`-whitelisted
    exception (a bare `RuntimeError`, not a whitelisted domain error) never
    leaks its own message, only the generic `"<stage> failed"` reason.
    Later stages never run.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(
        rid="cra-1.0", extract_error=RuntimeError("boom -- must never reach the caller")
    )
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: extraction stage failed: extraction failed"
    assert fake.recorder.order == ["ingestion", "extraction"]


def test_llm_interface_unhealthy_returns_named_error_without_opening_any_graph(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-PREFLIGHT: when `dependency_health.is_healthy(LLM_INTERFACE)` is
    `False`, the tool fails fast with the exact `handlers.py:72` message,
    before any graph is opened or the pipeline is called -- and the failure
    is still logged with the resolved principal.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )
    dependency_health.mark_unhealthy(dependency_health.LLM_INTERFACE, error=RuntimeError("down"))

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: LLM Interface is unavailable."
    assert fake.recorder.calls == []
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in lines)


def test_graph_unavailable_returns_named_error_when_native_graph_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `run_catalog_ingestion_pipeline` opens all three
    graphs before `_execute_catalog_stages`/`_run_stage` ever runs, with no
    try/except of its own -- a generic exception from any one of the three
    openers must sanitise to the same fixed message `_resolve_graph` already
    uses for `cypher`, never leaking host/port/driver detail, and no stage
    ever runs.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")

    def _raising_native(config: object, short_name: str) -> object:
        _ = (config, short_name)
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_dependencies = dataclasses.replace(
        fake.dependencies,
        graphs=dataclasses.replace(fake.dependencies.graphs, native=_raising_native),
    )
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: broken_dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"
    assert fake.recorder.calls == []


def test_collision_check_graph_unreachable_returns_graph_unavailable_error_without_opening_any_pipeline_graph(  # noqa: E501 - name mirrors the sibling graph-unavailable test's verbatim-scenario naming
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-007: `_resolve_and_ingest` opens the single-tenant
    graph itself (to run the collision check) before
    `run_catalog_ingestion_pipeline` ever runs -- that open is wrapped by the
    same `_sanitize_pipeline_graph_opens`/`_sanitize_graph_open` machinery
    `test_graph_unavailable_returns_named_error_when_native_graph_open_fails`
    already proves for the pipeline's own three graph opens, so a failure
    opening the single-tenant graph here must sanitise to the exact same
    fixed, generic message -- never leaking driver/host detail -- and no
    pipeline stage ever runs.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")

    def _raising_single_tenant(config: object) -> object:
        _ = config
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_dependencies = dataclasses.replace(
        fake.dependencies,
        graphs=dataclasses.replace(fake.dependencies.graphs, single_tenant=_raising_single_tenant),
    )
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: broken_dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"
    assert fake.recorder.calls == []


def test_collision_check_query_failure_surfaces_as_pipeline_stage_error_not_graph_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-007: when the single-tenant graph *opens* successfully
    but the collision-check *query itself* fails, that failure never touches
    `_sanitize_graph_open` (which only wraps the open) -- it is caught by
    `check_short_name_collision`'s own `_run_stage` wrapping inside
    `validate_and_resolve_catalog_entry`, becomes a `PipelineStageError`, and
    is caught by `_resolve_and_ingest`'s existing
    `except (..., PipelineStageError)` clause, surfacing as
    `f"error: {exc}"` with the real stage-failure text -- distinct from the
    generic `_GRAPH_UNAVAILABLE_MESSAGE` the graph-*open* failure mode
    produces in the sibling test above. No pipeline stage ever runs.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_pipeline_dependencies(
        rid="cra-1.0", collision_error=RuntimeError("boom -- must never reach the caller")
    )
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: collision_check stage failed: collision_check failed"
    assert fake.recorder.calls == []


def test_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4 / D-SANITIZE-UNEXPECTED's last row: an
    exception `ingest_regulation`'s own body does not itself sanitise (here,
    `routes._to_accepted_response` blowing up after a fully successful
    pipeline run) is caught by `_run_mcp_action`'s residual safety net --
    returned as the fixed, generic message (never the raw exception text),
    with the full `repr` logged server-side only.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    def _boom(run_id: str, outcome: object) -> object:
        _ = (run_id, outcome)
        raise RuntimeError("boom -- must never reach the caller")

    monkeypatch.setattr(mcp_server, "_to_accepted_response", _boom)

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert failed_line.get("principal") == LOCAL_TEST_PRINCIPAL_ID
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))
    assert "RuntimeError" in str(failed_line.get("detail"))


# --- Slice 1.4: auth/audit completeness under a real verified bearer token --


def _grant_compliance_officer(
    monkeypatch: pytest.MonkeyPatch, *, subject: str, issuer: str
) -> None:
    """Issue #145: seed a `ComplianceOfficer` grant for `(subject, issuer)` on a fake store,
    monkeypatched onto `mcp_server.PsycopgAccessRoleStore`.

    The two real-verified-token tests below predate issue #145's authz gate
    (they were written to prove principal/caller threading, not
    authorization) -- without this, the newly-added gate now denies them
    (or, in the LLM-unhealthy case, denies with the wrong error string)
    since the token's own subject holds no grant on the real store, which
    itself is unreachable in this test environment (`PS_AUTHZ_POSTGRES_HOST`
    unset) and would otherwise fail closed. Mirrors
    `test_catalog_source_authz_gate.py`'s own `_fake_store_factory` pattern.
    """
    store = FakeAccessRoleStore()
    store.grant(
        actor=(subject, issuer), target=(subject, issuer), access_role=AccessRole.COMPLIANCE_OFFICER
    )

    def _factory(_config: object, **_kwargs: object) -> object:
        return store

    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _factory)


def test_real_verified_token_principal_threads_through_to_both_audit_log_and_pipeline_caller(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """D-AUTH (a): under a real, verified bearer token -- driven through the
    actual mounted Streamable HTTP transport, mirroring `test_mcp_auth.py`'s
    own pattern, never a bare in-process `server.call_tool` -- the token's
    `sub` claim (never `LOCAL_TEST_PRINCIPAL_ID`) reaches both
    `_run_mcp_action`'s `mcp_interface` log entries (`principal`) and
    `run_catalog_ingestion_pipeline`'s own `ingestion_run` log entries
    (`caller`). Both were already wired from Slice 1.1's `principal =
    _resolve_principal(config)` / `caller=principal or "unknown"` plumbing --
    this is the first test to prove it under a real verified token rather
    than the local-test-bypass principal every other test in this module
    exercises.

    Issue #145: the caller must also hold `ComplianceOfficer` now that this
    tool is gated, so `_grant_compliance_officer` seeds that grant for this
    token's own `(sub, iss)` -- orthogonal to what this test itself proves.
    """
    _configure_complete_llm_env(monkeypatch)
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-ingest-42"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    _grant_compliance_officer(monkeypatch, subject=token_sub, issuer=auth_context.issuer)
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_ingest_regulation_over_http(
            client, token=token, celex=_CELEX, short_name=_SHORT_NAME
        )

    body = json.loads(result_text)
    assert body["regulatory_instrument_id"] == "cra-1.0"

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())

    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)
    assert not any(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)

    run_lines = [
        line
        for line in all_lines
        if line.get("component") == "api" and line.get("action") == "ingestion_run"
    ]
    assert len(run_lines) == 2
    assert all(line.get("caller") == token_sub for line in run_lines)


def test_local_test_bypass_principal_still_threads_through_when_no_token_is_presented(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUTH (b): `_resolve_principal`'s existing, unchanged fallback
    contract -- with no bearer token presented at all (a bare in-process
    `server.call_tool`, like every other test above) and the local-test
    bypass active, `LOCAL_TEST_PRINCIPAL_ID` still reaches both the
    `mcp_interface` log entries' `principal` (already proven by Slice 1.1's
    own test) and, newly proven here, `run_catalog_ingestion_pipeline`'s own
    `ingestion_run` log entries' `caller`.
    """
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())

    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in mcp_lines)

    run_lines = [
        line
        for line in all_lines
        if line.get("component") == "api" and line.get("action") == "ingestion_run"
    ]
    assert len(run_lines) == 2
    assert all(line.get("caller") == LOCAL_TEST_PRINCIPAL_ID for line in run_lines)


def test_failed_call_under_a_real_verified_token_still_carries_the_subs_principal(
    monkeypatch: pytest.MonkeyPatch, mock_oidc_provider: MockOidcProvider, read_lines: ReadLines
) -> None:
    """D-AUTH (c): a FAILED call -- reusing Slice 1.3's LLM-unhealthy
    D-PREFLIGHT branch -- still carries the resolved real-token principal on
    its `outcome="failed"` `mcp_interface` log entry, not only the
    succeeded-call case proven above. No `ingestion_run` entry is emitted at
    all here, since D-PREFLIGHT fails before the pipeline is ever called.

    Issue #145: the caller must hold `ComplianceOfficer` for this call to
    reach the D-PREFLIGHT branch at all -- see `_grant_compliance_officer`.
    """
    _configure_complete_llm_env(monkeypatch)
    emitter = configure()
    auth_context = _auth_context(mock_oidc_provider)
    token_sub = "user-ingest-failed-7"
    token = mock_oidc_provider.mint_token(sub=token_sub)
    _grant_compliance_officer(monkeypatch, subject=token_sub, issuer=auth_context.issuer)
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    monkeypatch.setattr(
        mcp_server, "build_default_pipeline_dependencies", lambda: fake.dependencies
    )
    dependency_health.mark_unhealthy(dependency_health.LLM_INTERFACE, error=RuntimeError("down"))

    with _authenticated_test_client(auth_context=auth_context) as client:
        result_text = _call_ingest_regulation_over_http(
            client, token=token, celex=_CELEX, short_name=_SHORT_NAME
        )

    assert result_text == "error: LLM Interface is unavailable."
    assert fake.recorder.calls == []

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "ingest_regulation"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "failed"]
    assert all(line.get("principal") == token_sub for line in mcp_lines)
    run_lines = [line for line in all_lines if line.get("action") == "ingestion_run"]
    assert run_lines == []
