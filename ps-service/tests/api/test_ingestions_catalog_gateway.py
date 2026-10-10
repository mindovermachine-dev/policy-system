"""``POST /ingestions`` (catalog) writes the native graph through the gateway (#208, slice 6).

The route's ingest stage is handed the process's Graph Write Gateway and the id of a system audit
row recorded for the run; with no gateway the run fails closed before any stage. The stages
themselves are the standard fake pipeline: what is proved here is the hand-off and the contract
of the response, which is the one the route returned before the migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from api._audit_fakes import InMemoryAuditStore
from api._fakes import build_fake_pipeline_dependencies, install_compliance_officer_grant
from ps_service.api.dependencies import provide_audit_store, provide_pipeline_dependencies
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    from api._fakes import FakePipeline
    from api._gateway_fakes import ProcessGateway

_BODY = {"source": "catalog", "celex": "32024R2847", "short_name": "cra"}


def _noop_emit(**_kwargs: object) -> None:
    """Discard a run-log entry (Logging boundary stub)."""


@pytest.fixture(autouse=True)
def _stub_run_log(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    # detroit-exception: process-wide atexit logging facade (AUDIT §2 case 12), not a business fake
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.emit_log_entry", _noop_emit)


@pytest.fixture(autouse=True)
def _grant(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    install_compliance_officer_grant(monkeypatch, granted=True)


def _client(fake: FakePipeline) -> TestClient:
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_similarity_threshold=0.83,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )
    app = create_app(config)
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    store = InMemoryAuditStore()
    app.dependency_overrides[provide_audit_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


def test_the_ingest_stage_is_handed_the_gateway_and_the_id_of_the_runs_system_audit_row(
    process_gateway: ProcessGateway,
) -> None:
    fake = build_fake_pipeline_dependencies()

    response = _client(fake).post("/ingestions", json=_BODY)

    assert response.status_code == 200
    kwargs = fake.recorder.calls[0].kwargs
    assert kwargs["submitter"] is process_gateway.rig.gateway
    assert process_gateway.system_audit.ids == [kwargs["audit_event_id"]]
    row = process_gateway.system_audit.rows[0]
    assert row["action"] == "ingestion_run.submit"
    assert row["resource_type"] == "ingestion_run"
    assert row["resource_id"] == response.json()["run_id"]
    assert row["details"] == {
        "status": "started",
        "trigger": "sync_ingest",
        "celex": "32024R2847",
        "short_name": "CRA",
    }


def test_the_response_is_the_one_the_route_returned_before_the_migration() -> None:
    response = _client(build_fake_pipeline_dependencies(rid="CRA-1.0")).post(
        "/ingestions", json=_BODY
    )

    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"run_id", "regulatory_instrument_id", "source", "stages", "outcome"}
    assert body["regulatory_instrument_id"] == "CRA-1.0"
    assert [stage["stage"] for stage in body["stages"]] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]


def test_without_a_gateway_the_run_fails_closed_before_any_stage_runs(
    process_gateway: ProcessGateway,
) -> None:
    install_graph_write_gateway(None)
    fake = build_fake_pipeline_dependencies()

    response = _client(fake).post("/ingestions", json=_BODY)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "graph_write_unavailable"
    assert fake.recorder.order == []
    assert process_gateway.system_audit.rows == []


def test_an_audit_outage_fails_closed_before_the_ingest_stage_writes_anything(
    process_gateway: ProcessGateway,
) -> None:
    unavailable = AuditTrailUnavailableError("The audit trail is temporarily unavailable.")
    install_graph_write_gateway(
        process_gateway.rig.gateway,
        system_audit=lambda _config: _raising(unavailable),
    )
    fake = build_fake_pipeline_dependencies()

    response = _client(fake).post("/ingestions", json=_BODY)

    assert response.status_code == 503
    assert fake.recorder.order == []


def _raising(error: Exception):
    def record(**_kwargs: object) -> str:
        raise error

    return record
