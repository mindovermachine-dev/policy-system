"""The imported policy tree's counts reach the response and the run log (issue #227, AC-BI-011).

``ingestion_run.complete`` has no internal-path row to carry them (it needs a CELEX and a
catalog/amendment trigger), so -- as restore does -- the four counts ride on the merge-stage
``summary`` (HTTP response) and on the ``succeeded`` ``ingestion_run`` log entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from graph_gateway._fakes import GatewayRig, close_all_rigs

from api._fakes import build_fake_pipeline_dependencies
from api._gateway_fakes import RecordingSystemAudit, override_gateway
from ps_service.api.dependencies import get_principal, provide_pipeline_dependencies
from ps_service.api.ingestion_orchestration import run_internal_ingestion_pipeline
from ps_service.auth.models import Principal
from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from api._fakes import MakeEmitter

_SEED: dict[str, object] = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "internal-sources"
        / "engineering-practices"
        / "engineering-practices-seed.json"
    ).read_text(encoding="utf-8")
)
_COUNTS = (11, 21, 39, 4)
_OWNER = ("owner-1", "https://idp")


def _config() -> ServiceConfig:
    """A loopback config with every pipeline-required value set."""
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


@pytest.fixture(autouse=True)
def _close_rigs() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Stop the reconciler thread of every gateway rig a test built."""
    yield
    close_all_rigs()


def test_merge_stage_summary_carries_the_governance_counts(configured_logging: Path) -> None:
    """The HTTP response's merge summary reports policies/standards/controls/overridden."""
    del configured_logging
    rig = GatewayRig()
    fake = build_fake_pipeline_dependencies(
        internal_rid="ENGPRAC-1.0", merge_governance_counts=_COUNTS
    )
    app = create_app(_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    app.dependency_overrides[get_principal] = lambda: Principal(sub=_OWNER[0], iss=_OWNER[1])
    override_gateway(app, rig, RecordingSystemAudit(rig))

    response = TestClient(app, raise_server_exceptions=False).post(
        "/ingestions", json={"source": "internal", "content": _SEED}
    )

    assert response.status_code == 200
    summary = response.json()["stages"][1]["summary"]
    assert (
        summary["governance_policies"],
        summary["governance_standards"],
        summary["governance_controls"],
        summary["governance_status_overridden"],
    ) == _COUNTS


def test_succeeded_run_log_entry_carries_the_governance_counts(
    make_emitter: MakeEmitter, read_lines: Callable[[Path], list[dict[str, object]]]
) -> None:
    """The ``succeeded`` ``ingestion_run`` log entry carries the same four counts."""
    emitter, log_path = make_emitter()
    rig = GatewayRig()
    fake = build_fake_pipeline_dependencies(
        internal_rid="ENGPRAC-1.0", merge_governance_counts=_COUNTS
    )

    run_internal_ingestion_pipeline(
        _SEED,
        config=_config(),
        run_id="run-1",
        caller="test",
        dependencies=fake.dependencies,
        gateway=rig.gateway,
        system_audit=RecordingSystemAudit(rig),
        owner=_OWNER,
        emitter=emitter,
    )
    emitter.flush()

    succeeded = next(
        entry
        for entry in read_lines(log_path)
        if entry["action"] == "ingestion_run" and entry["outcome"] == "succeeded"
    )
    extra = succeeded  # the log line carries ``extra`` keys flat
    assert (
        extra["governance_policies"],
        extra["governance_standards"],
        extra["governance_controls"],
        extra["governance_status_overridden"],
    ) == _COUNTS


def test_a_seed_without_governance_reports_zero_counts(
    make_emitter: MakeEmitter, read_lines: Callable[[Path], list[dict[str, object]]]
) -> None:
    """With nothing imported the succeeded entry reports all four counts as zero."""
    emitter, log_path = make_emitter()
    rig = GatewayRig()
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-1.0")

    run_internal_ingestion_pipeline(
        _SEED,
        config=_config(),
        run_id="run-2",
        caller="test",
        dependencies=fake.dependencies,
        gateway=rig.gateway,
        system_audit=RecordingSystemAudit(rig),
        owner=_OWNER,
        emitter=emitter,
    )
    emitter.flush()

    succeeded = next(
        entry
        for entry in read_lines(log_path)
        if entry["action"] == "ingestion_run" and entry["outcome"] == "succeeded"
    )
    extra = succeeded  # the log line carries ``extra`` keys flat
    assert extra["governance_policies"] == 0
    assert extra["governance_status_overridden"] == 0
