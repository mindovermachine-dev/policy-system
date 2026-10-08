"""Pre-flight parity characterization for the catalog-ingestion MCP tools (issue #194).

Pins, as exact returned text, the pre-flight error behaviour and its precedence
that `ingest_regulation` exhibits today: which named `error:` string wins when
two pre-flight failures coincide, and that no pipeline stage ever runs
(`fake.recorder.calls == []`). Written and run green against the unmodified
`mcp_server._resolve_and_ingest` before S1 extracted its validation/resolution
half into `_prepare_catalog_ingestion`, so this file is the proof that the
extraction changed nothing observable (AC-BI-018). It is parametrized over
`tool` so a later slice can add `"start_ingestion"` and prove the same
precedence holds for it (AC-BI-010) without re-declaring a single scenario.

Fixtures are imported from `test_ingest_regulation_tool.py` rather than
re-declared, mirroring `test_ingest_regulation_authz_gate.py`'s own
cross-module private-import convention, so this file can never drift from that
file's curated-catalog fixture.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING

import pytest
from api._fakes import build_fake_pipeline_dependencies
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from mcp_interface._ingestion_run_harness import (
    install_ingestion_run_store,
    isolate_ingestion_runs,
)
from mcp_interface.test_ingest_regulation_tool import (
    _CELEX,  # pyright: ignore[reportPrivateUsage]  -- reuse the curated-catalog fixture verbatim rather than re-declaring it, mirrors `test_ingest_regulation_authz_gate.py`'s own cross-module private-import convention
    _NONCURATED_CELEX,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SHORT_NAME,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _configure_complete_llm_env,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _use_fake_graph_openers_only,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service import dependency_health
from ps_service.ingestion.adapters.errors import CellarNotFoundError
from ps_service.ingestion_runs import dispatch
from ps_service.logging import configure, resolve_default_log_path
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Iterator

    from api._fakes import ReadLines
    from ingestion_runs._fakes import InMemoryIngestionRunStore

# The sync tool writes `ingestion_run.*` rows (issue #195): keep them off Postgres.
pytestmark = pytest.mark.usefixtures("ingest_audit_store")

_TOOLS = ("ingest_regulation", "start_ingestion")

_WRONG_SHORT_NAME = "not-the-real-short-name"
# Issue #193: identity is decided by the live graph and Cellar, never the curated catalog,
# so a CELEX already in the graph is rejected under any short_name.
_ALREADY_INGESTED_MESSAGE = f"error: CELEX {_CELEX} is already ingested as short_name 'CRA'"
_NOT_FOUND_MESSAGE = f"error: CELEX {_NONCURATED_CELEX!r} does not exist on Cellar/ELI."
_CONFIG_INCOMPLETE_MESSAGE = (
    "error: ingestion configuration incomplete: llm_interface_model, "
    "llm_interface_embed_model, company_merge_similarity_threshold not set"
)
_LLM_UNAVAILABLE_MESSAGE = "error: LLM Interface is unavailable."
_GRAPH_UNREACHABLE_MESSAGE = "error: the policy graph database is not reachable"


def _call(tool: str, celex: str, short_name: str) -> str:
    """Call `tool` in-process and return its single text block (never an MCP error)."""
    result = asyncio.run(
        mcp_server.server.call_tool(tool, {"celex": celex, "short_name": short_name})
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _configure_incomplete_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset all three fields `_require_ingestion_config` requires."""
    monkeypatch.delenv("PS_LLMINTERFACE_MODEL", raising=False)
    monkeypatch.delenv("PS_LLMINTERFACE_EMBED_MODEL", raising=False)
    monkeypatch.delenv("PS_COMPANYMERGE_SIMILARITY_THRESHOLD", raising=False)


@pytest.fixture(autouse=True)
def _bypass_and_logging(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture -- invoked by name-collection, never referenced in-module
    """Every scenario runs under the local-test bypass, so the gate never pre-empts it."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()


@pytest.fixture(autouse=True)
def _ingestion_run_store(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[InMemoryIngestionRunStore]:
    """Fake run store, so `start_ingestion` scenarios can assert no row/slot was created."""
    isolate_ingestion_runs()
    store = install_ingestion_run_store(monkeypatch)
    yield store
    isolate_ingestion_runs()


@pytest.fixture(autouse=True)
def _no_row_or_slot_after_a_preflight_failure(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture
    _ingestion_run_store: InMemoryIngestionRunStore,
) -> Iterator[None]:
    """AC-BI-010: whichever tool ran, a pre-flight failure leaves no row and no in-flight run."""
    yield
    assert _ingestion_run_store.rows == {}
    assert dispatch.in_flight_run_count() == 0


@pytest.mark.parametrize("tool", _TOOLS)
def test_already_ingested_celex_returns_already_ingested_error(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_complete_llm_env(monkeypatch)
    fake = build_fake_pipeline_dependencies(celex_row="CRA-1.0")
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    assert _call(tool, _CELEX, _WRONG_SHORT_NAME) == _ALREADY_INGESTED_MESSAGE
    assert fake.recorder.calls == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_already_ingested_celex_wins_over_incomplete_config(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_incomplete_llm_env(monkeypatch)
    fake = build_fake_pipeline_dependencies(celex_row="CRA-1.0")
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    assert _call(tool, _CELEX, _WRONG_SHORT_NAME) == _ALREADY_INGESTED_MESSAGE
    assert fake.recorder.calls == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_cellar_not_found_wins_over_incomplete_config(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_incomplete_llm_env(monkeypatch)
    fake = build_fake_pipeline_dependencies()
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    def _not_found_fetch(celex: str) -> bytes:
        raise CellarNotFoundError(f"CELEX {celex!r} was not found on Cellar/ELI")

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _not_found_fetch)

    assert _call(tool, _NONCURATED_CELEX, "some-short-name") == _NOT_FOUND_MESSAGE
    assert fake.recorder.calls == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_valid_curated_celex_with_incomplete_config_returns_config_error(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_incomplete_llm_env(monkeypatch)
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    assert _call(tool, _CELEX, _SHORT_NAME) == _CONFIG_INCOMPLETE_MESSAGE
    assert fake.recorder.calls == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_llm_unhealthy_wins_over_mismatched_short_name(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_complete_llm_env(monkeypatch)
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)
    dependency_health.mark_unhealthy(dependency_health.LLM_INTERFACE, error=RuntimeError("down"))

    assert _call(tool, _CELEX, _WRONG_SHORT_NAME) == _LLM_UNAVAILABLE_MESSAGE
    assert fake.recorder.calls == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_short_name_collision_with_already_ingested_celex_returns_collision_error(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_complete_llm_env(monkeypatch)
    given_short_name = "eu-1111-2020"
    fake = build_fake_pipeline_dependencies(collision_row=(f"{given_short_name}-1.0", "32024R0001"))
    _use_fake_graph_openers_only(monkeypatch, fake.dependencies.graphs)

    assert _call(tool, _NONCURATED_CELEX, given_short_name) == (
        f"error: short_name '{given_short_name.upper()}' is already claimed by CELEX 32024R0001"
    )
    assert fake.recorder.calls == []


@pytest.mark.parametrize("tool", _TOOLS)
def test_single_tenant_graph_open_failure_returns_graph_unreachable_error(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_complete_llm_env(monkeypatch)
    fake = build_fake_pipeline_dependencies(rid="cra-1.0")

    def _raising_single_tenant(config: object) -> object:
        _ = config
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_graphs = dataclasses.replace(
        fake.dependencies.graphs, single_tenant=_raising_single_tenant
    )
    _use_fake_graph_openers_only(monkeypatch, broken_graphs)

    assert _call(tool, _CELEX, _SHORT_NAME) == _GRAPH_UNREACHABLE_MESSAGE
    assert fake.recorder.calls == []


# --- Schema-layer parity (issue #194 S2, AC-BI-011) -------------------------


def _input_schemas() -> dict[str, dict[str, object]]:
    tools = asyncio.run(mcp_server.server.list_tools())
    return {tool.name: tool.input_schema for tool in tools}


def _properties(schema: dict[str, object]) -> dict[str, object]:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    return {str(key): value for key, value in properties.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def test_start_ingestion_input_schema_equals_ingest_regulations() -> None:
    schemas = _input_schemas()
    start, blocking = schemas["start_ingestion"], schemas["ingest_regulation"]

    assert _properties(start)["celex"] == _properties(blocking)["celex"]
    assert _properties(start)["short_name"] == _properties(blocking)["short_name"]
    assert start["required"] == blocking["required"]


@pytest.mark.parametrize("tool", ["ingest_regulation", "start_ingestion"])
@pytest.mark.parametrize(
    ("celex", "short_name"),
    [
        ("", _SHORT_NAME),
        ("3202", _SHORT_NAME),
        ("32024R28470", _SHORT_NAME),
        ("abcdefghij", _SHORT_NAME),
        ("4024R28470", _SHORT_NAME),
        (_CELEX, ""),
        (_CELEX, "1abc"),
        (_CELEX, "has space"),
        (_CELEX, "a" * 65),
        (_CELEX, "x;DROP"),
    ],
)
def test_malformed_arguments_are_rejected_by_the_schema_before_the_body_runs(
    tool: str,
    celex: str,
    short_name: str,
    monkeypatch: pytest.MonkeyPatch,
    read_lines: ReadLines,
) -> None:
    """AC-BI-011: identical `ToolError`; for `start_ingestion` no row, slot or log line."""
    isolate_ingestion_runs()
    runs = install_ingestion_run_store(monkeypatch)
    _configure_complete_llm_env(monkeypatch)
    emitter = configure()

    with pytest.raises(ToolError):
        _call(tool, celex, short_name)

    assert runs.rows == {}
    assert dispatch.in_flight_run_count() == 0
    emitter.flush()
    assert not [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("component") == "mcp_interface" and line.get("action") == tool
    ]
