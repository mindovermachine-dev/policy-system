"""``POST /ingestions`` (internal) refuses a governance-bearing seed with no verified caller.

An imported Policy tree is created ``draft`` and owned by the ingesting
caller, so a seed that carries a Policy and arrives without a verified identity is refused at
the door (401 ``owner_required``) before any stage, audit row, or graph write. A seed with no
Policy is not gated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from graph_gateway._fakes import GatewayRig

from api._fakes import build_fake_pipeline_dependencies
from api._gateway_fakes import RecordingSystemAudit, override_gateway
from ps_service.api.dependencies import get_principal, provide_pipeline_dependencies
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
_INSTRUMENT_ONLY_SEED: dict[str, object] = {
    "nodes": [
        {
            "label": "RegulatoryInstrument",
            "id": "TESTREG-1.0",
            "properties": {
                "title": "Test Regulation",
                "source_type": "internal",
                "effective_date": "2026-01-01",
                "version": "1.0",
                "status": "active",
            },
        }
    ],
    "edges": [],
}


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
        company_merge_similarity_threshold=0.83,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def test_ownerless_policy_bearing_seed_is_refused_with_401_before_anything_runs(
    rig: GatewayRig,
) -> None:
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    system_audit = RecordingSystemAudit(rig)
    app = create_app(_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    override_gateway(app, rig, system_audit)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/ingestions", json={"source": "internal", "content": _REAL_SEED})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "owner_required"
    assert fake.recorder.order == []
    assert rig.store.events == []
    assert system_audit.rows == []


def test_seed_without_a_policy_is_not_gated_when_ownerless(rig: GatewayRig) -> None:
    fake = build_fake_pipeline_dependencies(internal_rid="TESTREG-1.0")
    app = create_app(_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    override_gateway(app, rig, RecordingSystemAudit(rig))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/ingestions", json={"source": "internal", "content": _INSTRUMENT_ONLY_SEED}
    )

    assert response.status_code == 200


def test_policy_bearing_seed_with_a_verified_caller_is_accepted(rig: GatewayRig) -> None:
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    app = create_app(_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    app.dependency_overrides[get_principal] = lambda: Principal(sub="owner-1", iss="https://idp")
    override_gateway(app, rig, RecordingSystemAudit(rig))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/ingestions", json={"source": "internal", "content": _REAL_SEED})

    assert response.status_code == 200
