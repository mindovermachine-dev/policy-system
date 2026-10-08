"""`ingest_regulation` writes `ingestion_run.*` audit rows (issue #195, Slices 8 and 9).

The sync MCP tool delegates to `run_audited_catalog_ingestion`; these tests prove the transport
side: the actor (verified caller or the local-test-bypass sentinel), the bound run id as
`resource_id`, the counts against the real graph, and the `error: ` mapping of an unavailable
audit trail. Orchestration-level behaviour lives in
`tests/api/test_ingestion_orchestration_audit.py`.
"""

from __future__ import annotations

import json
from typing import cast

import pytest
from audit._fakes import InMemoryAuditStore, audit_store_factory

from mcp_interface.test_ingest_regulation_authz_gate import (
    _CALLER_ISSUER,  # pyright: ignore[reportPrivateUsage]  -- reuse the gate file's verified-caller fixtures verbatim
    _COMPLIANCE_OFFICER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _SYSTEM_OWNER_SUBJECT,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _fake_store_factory,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _seeded_store,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _verified_actor,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from mcp_interface.test_ingest_regulation_tool import (
    _CELEX,  # pyright: ignore[reportPrivateUsage]  -- reuse the curated-catalog fixture verbatim
    _SHORT_NAME,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _call_ingest_regulation,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _configure_complete_llm_env,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _text,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _use_real_pipeline_stages,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.logging import configure
from ps_service.mcp_interface import mcp_server

_BYPASS_ACTOR = ("system:local-test-bypass", "system:local-test-bypass")


@pytest.fixture(name="audit_store", autouse=True)
def _audit_store_fixture(monkeypatch: pytest.MonkeyPatch) -> InMemoryAuditStore:  # pyright: ignore[reportUnusedFunction]  # autouse + injected by name
    """Every sync ingest writes `ingestion_run.*` rows: keep them off Postgres."""
    store = InMemoryAuditStore()
    monkeypatch.setattr(mcp_server, "PsycopgAuditStore", audit_store_factory(store))
    return store


def _grant_compliance_officer(monkeypatch: pytest.MonkeyPatch) -> None:
    access = _seeded_store()
    access.grant(
        actor=(_SYSTEM_OWNER_SUBJECT, _CALLER_ISSUER),
        target=(_COMPLIANCE_OFFICER_SUBJECT, _CALLER_ISSUER),
        access_role=AccessRole.COMPLIANCE_OFFICER,
    )
    monkeypatch.setattr(mcp_server, "PsycopgAccessRoleStore", _fake_store_factory(access))


def test_ingest_regulation_audits_verified_actor_and_bound_run_id(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-001/004/005: both rows carry the verified caller and the response's run id."""
    _configure_complete_llm_env(monkeypatch)
    configure()
    _grant_compliance_officer(monkeypatch)
    _use_real_pipeline_stages(monkeypatch)

    with _verified_actor(sub=_COMPLIANCE_OFFICER_SUBJECT):
        result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert result.is_error is False
    run_id = json.loads(_text(result))["run_id"]
    assert [(r.action, r.resource_id, r.outcome) for r in audit_store.rows] == [
        ("ingestion_run.submit", run_id, "applied"),
        ("ingestion_run.complete", run_id, "applied"),
    ]
    assert {(r.actor_subject, r.actor_issuer) for r in audit_store.rows} == {
        (_COMPLIANCE_OFFICER_SUBJECT, _CALLER_ISSUER)
    }
    assert audit_store.rows[0].details["trigger"] == "sync_ingest"
    assert audit_store.rows[1].details["status"] == "succeeded"


def test_ingest_regulation_under_bypass_audits_sentinel(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-001: under the local-test bypass the actor is `system:local-test-bypass`."""
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _use_real_pipeline_stages(monkeypatch)

    _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert len(audit_store.rows) == 2
    assert {(r.actor_subject, r.actor_issuer) for r in audit_store.rows} == {_BYPASS_ACTOR}


def test_ingest_regulation_counts_equal_the_graph_delta(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-005/016: the complete row's counts equal what the real merge wrote to the graph."""
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fixture = _use_real_pipeline_stages(monkeypatch)

    def _count(label: str) -> int:
        rows = cast(
            "list[list[int]]",
            fixture.single_tenant.query(f"MATCH (n:{label}) RETURN count(n)").result_set,
        )
        return rows[0][0]

    _call_ingest_regulation(_CELEX, _SHORT_NAME)

    complete = audit_store.rows[1].details
    assert _count("Obligation") > 0
    assert complete["new_obligations"] == _count("Obligation")
    assert complete["new_capabilities"] == _count("Capability")
    assert complete["matched_capabilities"] == 0


def test_ingest_regulation_rows_are_retrievable_by_celex_and_instrument_id(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-018: the CELEX finds both rows; the instrument id finds the complete row only."""
    from ps_service.audit import AuditQueryFilters

    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _use_real_pipeline_stages(monkeypatch)

    _call_ingest_regulation(_CELEX, _SHORT_NAME)

    by_celex = audit_store.query(
        filters=AuditQueryFilters(details={"celex": _CELEX}), cursor=None, page_size=10
    )
    assert sorted(e.action for e in by_celex.events) == [
        "ingestion_run.complete",
        "ingestion_run.submit",
    ]
    by_instrument = audit_store.query(
        filters=AuditQueryFilters(details={"regulatory_instrument_id": "CRA-1.0"}),
        cursor=None,
        page_size=10,
    )
    assert [e.action for e in by_instrument.events] == ["ingestion_run.complete"]


def test_ingest_regulation_returns_error_prefix_when_audit_unavailable_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-011: an unwritable opening row is the fixed `error: ` text and nothing ran."""
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    fixture = _use_real_pipeline_stages(monkeypatch)
    audit_store.fail_on_outcome["applied"] = AuditPostgresUnavailableError("db.internal down")

    result = _call_ingest_regulation(_CELEX, _SHORT_NAME)

    assert _text(result) == (
        "error: The audit trail is temporarily unavailable; the operation was not performed."
    )
    assert "db.internal" not in _text(result)
    assert fixture.stage_order == []
    assert fixture.opened_short_names == []
    assert audit_store.rows == []


def test_re_ingesting_an_ingested_celex_audits_already_ingested_and_returns_the_same_error(
    monkeypatch: pytest.MonkeyPatch, audit_store: InMemoryAuditStore
) -> None:
    """AC-BI-006 / D-A: the `error:` is unchanged; a started/already_ingested pair is added."""
    _configure_complete_llm_env(monkeypatch)
    monkeypatch.setenv("PS_SERVICE_LOCAL_TEST_BYPASS", "true")
    configure()
    _use_real_pipeline_stages(monkeypatch)
    assert _call_ingest_regulation(_CELEX, _SHORT_NAME).is_error is False
    first_rows = len(audit_store.rows)

    second = _text(_call_ingest_regulation(_CELEX, _SHORT_NAME))

    assert second.startswith("error: ")
    new_rows = audit_store.rows[first_rows:]
    assert [(r.action, r.outcome) for r in new_rows] == [
        ("ingestion_run.submit", "applied"),
        ("ingestion_run.complete", "applied"),
    ]
    assert new_rows[1].details["outcome"] == "already_ingested"
    assert new_rows[1].details["new_obligations"] == 0
