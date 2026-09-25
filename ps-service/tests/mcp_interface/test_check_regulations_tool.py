"""Tests for the registered `check_regulations` MCP tool (issue #126, Slices 2.1-2.2).

Slice 2.1 covers the happy-path sweep: `check_regulations` (zero parameters
-- there is no AC-BI-005 format-validation surface for this tool, stated
explicitly here so its absence isn't mistaken for a gap) runs the D-PREFLIGHT
LLM-Interface-health check, then delegates in-process to
`run_change_check_sweep` (D-DELEGATE) via the shared `_run_mcp_action` audit
wrapper (D-AUDIT-WRAPPER), returning
`routes._to_change_check_response(result).model_dump()` (D-RESPONSE-SHAPE).

Slice 2.2 wires the remaining D-SANITIZE-UNEXPECTED rows for this tool: the
D-PREFLIGHT LLM-Interface-unhealthy check (proving it fires for this specific
tool, not just `ingest_regulation`), the graph-unavailable sanitiser around
`dependencies.open_single_tenant` (new production code:
`_sanitize_change_check_graph_opens`, mirroring `ingest_regulation`'s own
`_sanitize_pipeline_graph_opens` shape and reusing the same generic
`_sanitize_graph_open` per-opener wrapper -- not reinvented), and
`_run_mcp_action`'s own residual unexpected-exception safety net (D-AUDIT-
WRAPPER point 4), proven here specifically for `check_regulations` via a
fake `dependencies.read_tracked_instruments` raising something unclassified.

Hand-written structural fakes throughout -- no `unittest.mock` -- mirroring
`test_ingest_regulation_tool.py`'s/`test_cypher_tool.py`'s own convention.
The fake `ChangeCheckDependencies` bundle
(`tests/api/_fakes.py::build_fake_change_check_dependencies`) is the existing
fixture already used by the REST-side `run_change_check_sweep` tests
(`tests/api/test_change_check_orchestration.py`) -- reused here unchanged,
not reinvented, per PLAN.md's own instruction. No single existing REST-side
test scripts all six outcome buckets in one sweep, so this file's one test
assembles that scenario itself, from the same fixture, to prove
`check_regulations` reports every bucket verbatim.

`pytest-asyncio` is not installed; this file drives the tool with a bare
`asyncio.run(server.call_tool(...))`, exactly like `test_cypher_tool.py`/
`test_ingest_regulation_tool.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import date
from typing import TYPE_CHECKING

from api._fakes import build_fake_change_check_dependencies
from mcp.types import CallToolResult, TextContent

from ps_service import dependency_health
from ps_service.api.catalog import CatalogEntry
from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)
from ps_service.config import LOCAL_TEST_PRINCIPAL_ID
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.mcp_interface import mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    type ReadLines = Callable[[Path], list[dict[str, object]]]


class NationalTranspositionNotSupportedError(Exception):
    """Locally-defined test double proving D10's name-matching in
    `change_check_orchestration._reingest_one` (mirrors
    `test_routes_change_checks.py`'s own identically-named local double) --
    not an import of the real `ps_service.change_monitor.errors` type.
    """


def _call_check_regulations() -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("check_regulations", {}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_happy_path_sweep_reports_all_six_outcome_buckets_with_principal_logged(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """One `check_regulations` call, scripting all six outcome buckets in a
    single sweep: `current`, `poll_failed`, `not_configured`,
    `amendment_reingested`, `skipped`, `reingest_failed`. Asserts the
    returned dict's `instruments` list carries all six verbatim, plus the
    `mcp_interface` started/succeeded log pair carrying the resolved
    principal (D-AUTH's stated gap-closing behavior for this tool, since
    `run_change_check_sweep` itself takes no `caller` parameter at all).
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()

    tracked = (
        TrackedInstrumentNode(
            regulatory_instrument_id="id_current",
            celex="32024R0001",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
        TrackedInstrumentNode(
            regulatory_instrument_id="id_poll_failed",
            celex="32024R0002",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
        TrackedInstrumentNode(
            regulatory_instrument_id="id_not_configured",
            celex="32024R0003",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
        TrackedInstrumentNode(
            regulatory_instrument_id="id_amendment",
            celex="32024R0004",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
        TrackedInstrumentNode(
            regulatory_instrument_id="id_skipped",
            celex="32024R0005",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
        TrackedInstrumentNode(
            regulatory_instrument_id="id_reingest_failed",
            celex="32024R0006",
            instrument_type="regulation",
            effective_date="2024-01-01",
        ),
    )
    finding_amendment = AmendmentFinding(
        regulatory_instrument_id="id_amendment",
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R0004C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
    finding_skipped = AmendmentFinding(
        regulatory_instrument_id="id_skipped",
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R0005C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
    finding_reingest_failed = AmendmentFinding(
        regulatory_instrument_id="id_reingest_failed",
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R0006C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
    poll_report = PollReport(
        findings=(finding_amendment, finding_skipped, finding_reingest_failed),
        polled_count=6,
        failed_ids=("id_poll_failed",),
        unconfigured_ids=("id_not_configured",),
    )
    entry_amendment = CatalogEntry(
        celex="32024R0004", title="Amendment Regulation", short_name="amend-reg", version="1.0"
    )
    entry_skipped = CatalogEntry(
        celex="32024R0005", title="Skipped Regulation", short_name="skip-reg", version="1.0"
    )
    # Deliberately no catalog entry for "32024R0006" -- the D5 "no curated
    # catalog entry" path, driving id_reingest_failed's `reingest_failed`
    # outcome without needing a scripted `trigger_reingestion` failure.
    catalog_entries = {"32024R0004": entry_amendment, "32024R0005": entry_skipped}
    reingestion_outcome = ReingestionOutcome(
        prior_regulatory_instrument_id="id_amendment-0.9",
        new_regulatory_instrument_id="id_amendment-1.0",
        run_id="reingest-run-amend-1",
        outcome="superseded",
        ingest_counts={},
    )
    skip_exc = NationalTranspositionNotSupportedError(
        "Re-ingestion of a national_transposition instrument is not supported..."
    )
    # trigger_reingestion is only ever called for id_amendment and id_skipped
    # (id_reingest_failed short-circuits before it per D5), in tracked-list
    # order -- id_amendment first, id_skipped second.
    fake = build_fake_change_check_dependencies(
        tracked=tracked,
        poll_report=poll_report,
        catalog_entries=catalog_entries,
        reingestion_results=(reingestion_outcome, skip_exc),
    )
    monkeypatch.setattr(
        mcp_server, "build_default_change_check_dependencies", lambda: fake.dependencies
    )

    result = _call_check_regulations()

    assert result.is_error is False
    body = json.loads(_text(result))
    assert body["run_id"]
    assert isinstance(body["run_id"], str)
    outcomes = {entry["instrument_id"]: entry for entry in body["instruments"]}
    assert set(outcomes) == {
        "id_current",
        "id_poll_failed",
        "id_not_configured",
        "id_amendment",
        "id_skipped",
        "id_reingest_failed",
    }
    assert outcomes["id_current"]["outcome"] == "current"
    assert outcomes["id_poll_failed"]["outcome"] == "poll_failed"
    assert outcomes["id_not_configured"]["outcome"] == "not_configured"
    assert outcomes["id_amendment"]["outcome"] == "amendment_reingested"
    assert outcomes["id_amendment"]["reingest_run_id"] == "reingest-run-amend-1"
    assert outcomes["id_amendment"]["detail"] == "id_amendment-1.0 (superseded)"
    assert outcomes["id_skipped"]["outcome"] == "skipped"
    assert outcomes["id_skipped"]["detail"] == str(skip_exc)
    assert outcomes["id_reingest_failed"]["outcome"] == "reingest_failed"
    assert (
        outcomes["id_reingest_failed"]["detail"] == "no curated catalog entry for CELEX 32024R0006"
    )

    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    mcp_lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "check_regulations"
    ]
    assert [line["outcome"] for line in mcp_lines] == ["started", "succeeded"]
    assert all(line["run_id"] for line in mcp_lines)
    assert len({line["run_id"] for line in mcp_lines}) == 1
    for line in mcp_lines:
        assert line.get("principal") == LOCAL_TEST_PRINCIPAL_ID


# --- Slice 2.2: remaining D-SANITIZE-UNEXPECTED rows for this tool ----------


def test_llm_interface_unhealthy_returns_named_error_without_opening_any_graph(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-PREFLIGHT: when `dependency_health.is_healthy(LLM_INTERFACE)` is
    `False`, `check_regulations` fails fast with the exact `handlers.py:72`
    message, before any graph is opened or the sweep is run -- and the
    failure is still logged with the resolved principal. Mirrors
    `test_ingest_regulation_tool.py`'s identically-named test for this same
    D-PREFLIGHT branch, proven here specifically for `check_regulations`.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    fake = build_fake_change_check_dependencies()
    monkeypatch.setattr(
        mcp_server, "build_default_change_check_dependencies", lambda: fake.dependencies
    )
    dependency_health.mark_unhealthy(dependency_health.LLM_INTERFACE, error=RuntimeError("down"))

    result = _call_check_regulations()

    assert result.is_error is False
    assert _text(result) == "error: LLM Interface is unavailable."
    assert fake.read_tracked_instruments_graphs == []
    assert fake.poll_for_amendments_graphs == []
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "check_regulations"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    assert all(line.get("principal") == LOCAL_TEST_PRINCIPAL_ID for line in lines)


def test_graph_unavailable_returns_named_error_when_single_tenant_graph_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-SANITIZE-UNEXPECTED: `run_change_check_sweep` calls
    `dependencies.open_single_tenant` directly, with no try/except of its
    own -- a generic exception from that opener must sanitise to the same
    fixed message `_resolve_graph`/`ingest_regulation` already use, never
    leaking host/port/driver detail, via the new
    `_sanitize_change_check_graph_opens` wrapper (reusing the existing
    generic `_sanitize_graph_open` per-opener helper, not reinventing it).
    No tracked instrument is ever read.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fake = build_fake_change_check_dependencies()

    def _raising_open_single_tenant(config: object) -> object:
        _ = config
        message = "connection refused to 10.0.0.1:6379"  # must never reach the caller
        raise ConnectionError(message)

    broken_dependencies = dataclasses.replace(
        fake.dependencies, open_single_tenant=_raising_open_single_tenant
    )
    monkeypatch.setattr(
        mcp_server, "build_default_change_check_dependencies", lambda: broken_dependencies
    )

    result = _call_check_regulations()

    assert result.is_error is False
    assert _text(result) == "error: the policy graph database is not reachable"
    assert fake.read_tracked_instruments_graphs == []
    assert fake.poll_for_amendments_graphs == []


def test_residual_unexpected_exception_returns_generic_error_and_logs_detail(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    """D-AUDIT-WRAPPER point 4 / D-SANITIZE-UNEXPECTED's last row: an
    exception `check_regulations`'s own body does not itself sanitise (here,
    `dependencies.read_tracked_instruments` raising something unclassified,
    with the single-tenant graph already successfully opened) is caught by
    `_run_mcp_action`'s residual safety net -- returned as the fixed,
    generic message (never the raw exception text), with the full `repr`
    logged server-side only. Mirrors
    `test_ingest_regulation_tool.py`'s identically-named test, proven here
    specifically for `check_regulations`.
    """
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    fake = build_fake_change_check_dependencies()

    def _raising_read_tracked_instruments(graph: object) -> object:
        _ = graph
        raise ValueError("boom -- must never reach the caller")

    broken_dependencies = dataclasses.replace(
        fake.dependencies, read_tracked_instruments=_raising_read_tracked_instruments
    )
    monkeypatch.setattr(
        mcp_server, "build_default_change_check_dependencies", lambda: broken_dependencies
    )

    result = _call_check_regulations()

    assert result.is_error is False
    assert _text(result) == "error: an unexpected error occurred"
    emitter.flush()
    all_lines = read_lines(resolve_default_log_path())
    lines = [
        line
        for line in all_lines
        if line.get("component") == "mcp_interface" and line.get("action") == "check_regulations"
    ]
    assert [line["outcome"] for line in lines] == ["started", "failed"]
    failed_line = lines[-1]
    assert failed_line.get("principal") == LOCAL_TEST_PRINCIPAL_ID
    assert "boom -- must never reach the caller" in str(failed_line.get("detail"))
    assert "ValueError" in str(failed_line.get("detail"))
