"""The owner guard for a governance-bearing internal seed.

An imported Policy tree is created ``draft`` and owned by the ingesting caller, so the pipeline
refuses a seed that carries a Policy when it is given no owner, before any stage, audit row or
graph write. Over HTTP the role gate rejects a caller without a verified identity first, so the
guard is exercised at the orchestration function.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD
from fastapi.testclient import TestClient
from graph_gateway._fakes import GatewayRig

from api._fakes import build_fake_pipeline_dependencies, install_compliance_officer_caller
from api._gateway_fakes import RecordingSystemAudit, override_gateway
from ps_service.api.dependencies import provide_pipeline_dependencies
from ps_service.api.errors import InternalIngestOwnerRequiredError
from ps_service.api.ingestion_orchestration import run_internal_ingestion_pipeline
from ps_service.auth.models import Principal
from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

_SEED_PATH = (
    Path(__file__).resolve().parents[3]
    / "internal-sources"
    / "engineering-practices"
    / "engineering-practices-seed.json"
)
_REAL_SEED: dict[str, object] = json.loads(_SEED_PATH.read_text(encoding="utf-8"))


def _noop_emit(**_kwargs: object) -> None:
    """Discard a run-log entry (Logging boundary stub)."""


@pytest.fixture(autouse=True)
def _stub_run_log(  # pyright: ignore[reportUnusedFunction]  # autouse
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the process-wide logging facade, as every route test of this package does."""
    # detroit-exception: process-wide atexit logging facade, not a business fake
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.emit_log_entry", _noop_emit)


@pytest.fixture
def rig() -> Iterator[GatewayRig]:
    gateway_rig = GatewayRig()
    yield gateway_rig
    gateway_rig.close()


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_capability_merge_threshold=0.83,
        company_merge_capability_review_threshold=REVIEW_THRESHOLD,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def test_policy_bearing_seed_without_an_owner_is_refused_before_any_stage_runs(
    rig: GatewayRig,
) -> None:
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    system_audit = RecordingSystemAudit(rig)

    with pytest.raises(InternalIngestOwnerRequiredError):
        run_internal_ingestion_pipeline(
            _REAL_SEED,
            config=_config(),
            run_id="run-owner-guard",
            caller="127.0.0.1",
            dependencies=fake.dependencies,
            gateway=rig.gateway,
            system_audit=system_audit,
            owner=None,
        )

    assert fake.recorder.order == []
    assert rig.store.events == []
    assert system_audit.rows == []


def test_policy_bearing_seed_with_a_verified_caller_is_accepted(
    rig: GatewayRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    app = create_app(_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    install_compliance_officer_caller(
        monkeypatch, app, principal=Principal(sub="owner-1", iss="https://idp")
    )
    override_gateway(app, rig, RecordingSystemAudit(rig))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/ingestions", json={"source": "internal", "content": _REAL_SEED})

    assert response.status_code == 200
