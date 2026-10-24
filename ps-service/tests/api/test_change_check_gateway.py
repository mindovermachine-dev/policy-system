"""The amendment sweep resolves the Graph Write Gateway first.

The sweep writes the re-ingest succession through the gateway, including a `finalize` or
link-only re-ingest that runs no pipeline stage. So the gateway is resolved unconditionally,
before the `started` log entry and before any graph is opened: with none installed the sweep
raises `GraphWriteUnavailableError` and nothing was opened, read, logged or written.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent

from api._audit_fakes import InMemoryAuditStore
from api._fakes import (
    build_fake_change_check_dependencies,
    install_compliance_officer_grant,
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import run_change_check_sweep
from ps_service.api.dependencies import provide_audit_store, provide_change_check_dependencies
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.audit import AuditContext
from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)
from ps_service.config import ServiceConfig
from ps_service.logging import configure
from ps_service.logging.facade import resolve_default_log_path
from ps_service.main import create_app
from ps_service.mcp_interface import ingestion_tools, mcp_server

if TYPE_CHECKING:
    from collections.abc import Callable

    from api._fakes import FakeChangeCheckDependencies, MakeEmitter, ReadLines

_CELEX = "32024R2847"
_NEW_VERSION = "32024R2847C01"
_NODE = TrackedInstrumentNode(
    regulatory_instrument_id="CRA-1.0",
    celex=_CELEX,
    instrument_type="regulation",
    effective_date="2024-01-01",
)
_FINDING = AmendmentFinding(
    regulatory_instrument_id="CRA-1.0",
    instrument_type="regulation",
    baseline_reference="2024-01-01",
    detected_consolidated_celex=_NEW_VERSION,
    detected_consolidation_date=date(2025, 1, 1),
    reason="newer_consolidation",
)
_APP_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=True,
    authentik_api_token="test-authentik-token",
    authentik_base_url="https://authentik.example.com",
)


def _no_findings() -> FakeChangeCheckDependencies:
    return build_fake_change_check_dependencies(tracked=(_NODE,))


def _link_only_finding() -> FakeChangeCheckDependencies:
    """A finding whose re-ingest runs no stage (`finalize` / link-only): the probe answers False."""
    outcome = ReingestionOutcome("CRA-1.0", f"CRA-{_NEW_VERSION}", None, "superseded", "finalize")
    return build_fake_change_check_dependencies(
        tracked=(_NODE,),
        poll_report=PollReport(
            findings=(_FINDING,), polled_count=1, failed_ids=(), unconfigured_ids=()
        ),
        catalog_entries={
            _CELEX: CatalogEntry(celex=_CELEX, title="CRA", short_name="CRA", version="1.0")
        },
        reingestion_result=outcome,
    )


@pytest.mark.parametrize("build", [_no_findings, _link_only_finding], ids=["current", "link_only"])
def test_without_a_gateway_the_sweep_fails_before_any_graph_is_opened_or_logged(
    build: Callable[[], FakeChangeCheckDependencies],
    make_emitter: MakeEmitter,
    read_lines: ReadLines,
    app_config: ServiceConfig,
) -> None:
    fake = build()
    emitter, log_path = make_emitter()
    install_graph_write_gateway(None)

    with pytest.raises(GraphWriteUnavailableError):
        run_change_check_sweep(
            config=app_config,
            run_id="sweep",
            dependencies=fake.dependencies,
            audit=AuditContext(("caller", "https://issuer.example.com/"), InMemoryAuditStore()),
            emitter=emitter,
        )

    emitter.flush()
    assert fake.read_tracked_instruments_graphs == []
    assert fake.poll_for_amendments_graphs == []
    assert fake.open_native_short_names == []
    assert fake.trigger_reingestion_calls == []
    assert read_lines(log_path) == []


def test_post_change_checks_without_a_gateway_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    install_compliance_officer_grant(monkeypatch, granted=True)
    fake = _no_findings()
    app = create_app(_APP_CONFIG)
    app.dependency_overrides[provide_change_check_dependencies] = lambda: fake.dependencies
    store = InMemoryAuditStore()
    app.dependency_overrides[provide_audit_store] = lambda: store
    install_graph_write_gateway(None)

    response = TestClient(app, raise_server_exceptions=False).post("/change-checks")

    assert response.status_code == 503
    assert fake.read_tracked_instruments_graphs == []


def test_check_regulations_without_a_gateway_answers_with_an_error_string(
    monkeypatch: pytest.MonkeyPatch, read_lines: ReadLines
) -> None:
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    emitter = configure()
    fake = _no_findings()
    monkeypatch.setattr(
        ingestion_tools, "build_default_change_check_dependencies", lambda: fake.dependencies
    )
    install_graph_write_gateway(None)

    import asyncio

    result = asyncio.run(mcp_server.server.call_tool("check-regulations", {}))

    assert isinstance(result, CallToolResult)
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text.startswith("error: ")
    assert "graph writes are unavailable" in block.text
    assert fake.read_tracked_instruments_graphs == []
    emitter.flush()
    sweep_lines = [
        line
        for line in read_lines(resolve_default_log_path())
        if line.get("component") == "api" and line.get("action") == "change_check_sweep"
    ]
    assert sweep_lines == []
