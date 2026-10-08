"""Cross-transport parity of the new audit rows (issue #195, AC-BI-017 / AC-BI-001).

The same operation over MCP, REST or a passkey approval records the same rows (apart from the
actor and the run id). Per-operation parity is pinned next to each operation
(`test_ingestion_parity`, `test_restore_parity`, `test_near_miss_parity`,
`test_merge_audit`); this file adds the one operation whose two transports are only reachable
from here (the `check_regulations` sweep, over the real MCP tool AND the real REST route) and the
structural guarantees that make a silently unaudited transport impossible.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import date

import pytest
from api._audit_fakes import InMemoryAuditStore as RestAuditStore
from api._audit_fakes import RecordedAuditRow
from api._fakes import (
    FakeChangeCheckDependencies,
    build_fake_change_check_dependencies,
    install_compliance_officer_grant,
)
from audit._fakes import InMemoryAuditStore, audit_store_factory
from fastapi.testclient import TestClient

from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import run_change_check_sweep
from ps_service.api.dependencies import (
    provide_audit_store,
    provide_change_check_dependencies,
)
from ps_service.api.ingestion_orchestration import run_audited_catalog_ingestion
from ps_service.api.near_miss_review_orchestration import run_resolve_near_miss
from ps_service.api.restore_orchestration import (
    run_restoration,
    run_restoration_from_catalog_source,
)
from ps_service.audit import AuditActorUnresolvedError, resolve_audit_actor
from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)
from ps_service.config import ServiceConfig
from ps_service.invitations.service import invite_user_audited
from ps_service.logging import configure
from ps_service.main import create_app
from ps_service.mcp_interface import mcp_server

_BYPASS_ACTOR = ("system:local-test-bypass", "system:local-test-bypass")


def _amended_fake() -> FakeChangeCheckDependencies:
    node = TrackedInstrumentNode(
        regulatory_instrument_id="CRA-1.0",
        celex="32024R2847",
        instrument_type="regulation",
        effective_date="2024-01-01",
    )
    finding = AmendmentFinding(
        regulatory_instrument_id="CRA-1.0",
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R2847C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
    return build_fake_change_check_dependencies(
        tracked=(node,),
        poll_report=PollReport(
            findings=(finding,), polled_count=1, failed_ids=(), unconfigured_ids=()
        ),
        catalog_entries={
            "32024R2847": CatalogEntry(
                celex="32024R2847", title="CRA", short_name="CRA", version="1.0"
            )
        },
        reingestion_result=ReingestionOutcome(
            prior_regulatory_instrument_id="CRA-0.9",
            new_regulatory_instrument_id="CRA-1.0",
            run_id="ingest-run-1",
            outcome="superseded",
            ingest_counts={},
        ),
    )


def _shape(rows: list[RecordedAuditRow]) -> list[tuple[object, ...]]:
    return [(r.action, r.resource_type, r.outcome, r.details) for r in rows]


def test_mcp_and_rest_sweeps_record_the_same_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-BI-017: one amended instrument -> identical submit/complete rows over both transports."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    mcp_store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(mcp_store))
    fake_mcp = _amended_fake()
    monkeypatch.setattr(
        mcp_server,
        "build_default_change_check_dependencies",
        lambda: fake_mcp.dependencies,
    )
    asyncio.run(mcp_server.server.call_tool("check_regulations", {}))

    install_compliance_officer_grant(monkeypatch, granted=True)
    rest_store = RestAuditStore()
    app = create_app(
        ServiceConfig(
            host="127.0.0.1",
            port=8000,
            graceful_shutdown_seconds=10,
            logging_dir=None,
            is_local_test_bypass_active=True,
            authentik_api_token="test-authentik-token",
            authentik_base_url="https://authentik.example.com",
        )
    )
    fake_rest = _amended_fake()
    app.dependency_overrides[provide_change_check_dependencies] = lambda: fake_rest.dependencies
    app.dependency_overrides[provide_audit_store] = lambda: rest_store
    response = TestClient(app).post("/change-checks")

    assert response.status_code == 200
    assert len(mcp_store.rows) == len(rest_store.rows) == 2
    assert _shape(rest_store.rows) == _shape([RecordedAuditRow(**vars(r)) for r in mcp_store.rows])


@pytest.mark.parametrize(
    "emitter",
    [
        run_audited_catalog_ingestion,
        run_change_check_sweep,
        run_restoration,
        run_restoration_from_catalog_source,
        run_resolve_near_miss,
        invite_user_audited,
    ],
)
def test_every_new_emitter_requires_an_actor_argument(emitter: object) -> None:
    """No emitter can be called without saying who acts (so a transport cannot forget it)."""
    parameter = inspect.signature(emitter).parameters["audit"]  # pyright: ignore[reportArgumentType]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_every_new_emitter_uses_the_system_local_test_bypass_actor_under_bypass() -> None:
    """The one place REST and MCP resolve an actor: bypass -> sentinel, otherwise the caller."""
    assert resolve_audit_actor(None, is_local_test_bypass_active=True) == _BYPASS_ACTOR
    assert resolve_audit_actor(("sub", "iss"), is_local_test_bypass_active=True) == ("sub", "iss")


def test_no_actor_and_no_bypass_is_refused_instead_of_attributed_to_the_sentinel() -> None:
    with pytest.raises(AuditActorUnresolvedError):
        resolve_audit_actor(None, is_local_test_bypass_active=False)
