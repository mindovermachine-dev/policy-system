"""`check_regulations` writes `ingestion_run.*` audit rows per re-ingest (issue #195, Slice 10).

Transport side only: the actor (verified caller or the local-test-bypass sentinel), the
re-ingest's own run id as `resource_id`, and the `error: ` mapping of an unavailable audit
trail. The sweep's row shapes live in `tests/api/test_change_check_orchestration_audit.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from api._fakes import build_fake_change_check_dependencies
from audit._fakes import InMemoryAuditStore, audit_store_factory
from mcp.types import CallToolResult, TextContent

from mcp_interface.test_check_regulations_authz_gate import (
    _CALLER_ISSUER,  # pyright: ignore[reportPrivateUsage]  -- reuse the gate file's verified-caller fixtures verbatim
    _COMPLIANCE_OFFICER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.api.catalog import CatalogEntry
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.change_monitor.models import (
    AmendmentFinding,
    PollReport,
    ReingestionOutcome,
    TrackedInstrumentNode,
)
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

_BYPASS_ACTOR = ("system:local-test-bypass", "system:local-test-bypass")


@pytest.fixture(name="audit_store", autouse=True)
def _audit_store_fixture(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:  # pyright: ignore[reportUnusedFunction]  # autouse + injected by name
    """Keep the audit rows off Postgres."""
    store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    return store


@pytest.fixture(autouse=True)
def _amended_instrument(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    """One tracked instrument with a detected amendment that re-ingests fresh."""
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
    fake = build_fake_change_check_dependencies(
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
        ),
    )
    monkeypatch.setattr(
        mcp_server, "build_default_change_check_dependencies", lambda: fake.dependencies
    )


def _call() -> CallToolResult:
    result = asyncio.run(mcp_server.server.call_tool("check_regulations", {}))
    assert isinstance(result, CallToolResult)
    return result


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_check_regulations_audits_the_caller_as_actor(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-001/007: both rows carry the verified caller and a re-ingest run id."""
    configure()
    access = _seeded_store()
    access.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_COMPLIANCE_OFFICER_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(access))

    with _verified_actor(sub=_COMPLIANCE_OFFICER_SUBJECT):
        result = _call()

    assert result.is_error is False
    assert [(r.action, r.outcome) for r in audit_store.rows] == [
        ("ingestion_run.submit", "applied"),
        ("ingestion_run.complete", "applied"),
    ]
    assert {(r.actor_subject, r.actor_issuer) for r in audit_store.rows} == {
        (_COMPLIANCE_OFFICER_SUBJECT, _CALLER_ISSUER)
    }
    assert audit_store.rows[0].details["trigger"] == "amendment_check"
    assert audit_store.rows[0].resource_id == audit_store.rows[1].resource_id
    assert audit_store.rows[0].resource_id != json.loads(_text(result))["run_id"]


def test_check_regulations_under_bypass_audits_sentinel(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-001: under the local-test bypass the actor is `system:local-test-bypass`."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()

    _call()

    assert len(audit_store.rows) == 2
    assert {(r.actor_subject, r.actor_issuer) for r in audit_store.rows} == {_BYPASS_ACTOR}


def test_check_regulations_returns_error_prefix_when_audit_unavailable(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-011: an unwritable opening row yields an `error: ` string and no re-ingest."""
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    audit_store.fail_on_outcome["applied"] = AuditPostgresUnavailableError("db down")

    result = _call()

    assert _text(result).startswith("error: ")
    assert "db down" not in _text(result)
    assert audit_store.rows == []
