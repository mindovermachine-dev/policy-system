"""HTTP tests for ``POST /ingestions`` -- the internal-document ingestion path (issue #54, S2).

Replaces the now-deleted ``test_internal_request_returns_501_referencing_54``
(``tests/api/test_ingestions_catalog.py``): a ``source: "internal"`` request no
longer 501s -- it carries the intake document's content directly in the
request body (issue #91 -- no server-side path resolution) and runs the
internal-seed pipeline's stages (``internal_ingestion``, ``merge``), via the
same ``app.dependency_overrides`` fake ``PipelineDependencies`` pattern every
other route test in this package uses (``tests/api/_fakes.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from api._fakes import build_fake_pipeline_dependencies
from ps_service.api.dependencies import provide_pipeline_dependencies
from ps_service.config import ServiceConfig
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.main import create_app

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import PipelineDependencies

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REAL_SEED_DOCUMENT: dict[str, object] = json.loads(
    (
        _REPO_ROOT
        / "internal-sources"
        / "engineering-practices"
        / "engineering-practices-seed.json"
    ).read_text(encoding="utf-8")
)
_ENVELOPE_CONTRACT: dict[str, object] = json.loads(
    (_REPO_ROOT / "test-data" / "wire-contracts" / "ingest-internal-envelope.json").read_text(
        encoding="utf-8"
    )
)


def _noop_emit(**_kwargs: object) -> None:
    """Discard a run-log entry (Logging boundary stub -- mirrors test_ingestions_catalog.py)."""


@pytest.fixture(autouse=True)
def _stub_run_log(  # pyright: ignore[reportUnusedFunction]  # module autouse fixture — invoked by name-collection
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the Logging boundary so the pipeline needs no ``configure()``d facade.

    See ``test_ingestions_catalog.py``'s identical fixture for the full
    rationale -- ``run_internal_ingestion_pipeline`` emits its own
    ``ingestion_run`` entries through the same process-wide default emitter.
    """
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.emit_log_entry", _noop_emit)


def _app_config() -> ServiceConfig:
    """A loopback config with every pipeline-required value set (so the 503 guard never trips)."""
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_model="azure/gpt-4o",
        llm_interface_embed_model="azure/text-embedding-3-large",
        company_merge_similarity_threshold=0.83,
    )


def _client_with_fake(fake_deps: PipelineDependencies) -> TestClient:
    """A ``TestClient`` whose ``provide_pipeline_dependencies`` yields ``fake_deps``."""
    app = create_app(_app_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake_deps
    return TestClient(app, raise_server_exceptions=False)


def test_post_ingestions_internal_runs_real_pipeline() -> None:
    """A well-formed internal request runs the real ``internal_ingestion`` and
    ``merge`` stages (GH #76 removed the ``governance_derivation`` stage
    outright -- Policy/Standard/Control are now authored directly in the
    submitted document and minted by ``internal_ingestion`` itself; issue
    #54 S4's ``merge`` stage still closes the loop to ``policy_system``).

    ``content`` is the parsed ``engineering-practices-seed.json`` fixture
    (B1), read from ``test-data/`` at test-collection time -- the fake
    adapter delegates to the real, already-tested
    ``InternalSeedIngestionAdapter.parse_seed`` so this test exercises real
    schema/parse validation, while the persistence/merge stages themselves stay
    faked (no real FalkorDB/LLM reached, matching every other route test in
    this package).
    """
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": _ENVELOPE_CONTRACT["source"],
            _ENVELOPE_CONTRACT["content_field_name"]: _REAL_SEED_DOCUMENT,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["regulatory_instrument_id"] == "ENGPRAC-3.0"
    assert body["source"] == "internal"
    assert [stage["stage"] for stage in body["stages"]] == ["internal_ingestion", "merge"]
    assert body["stages"][0]["status"] == "succeeded"
    assert body["stages"][1]["status"] == "succeeded"
    assert fake.recorder.order == ["internal_ingestion", "merge"]


def test_post_ingestions_internal_summary_reports_policies_key() -> None:
    """AC-BI-010's structural half, now fully satisfied: the ``internal_ingestion``
    stage's summary dict contains ``"policies"`` (GH #76 Slice 1), ``"standards"``
    (GH #76 Slice 2), and ``"controls"`` (GH #76 Slice 3) keys -- and the
    pipeline's own stage list is exactly ``["internal_ingestion", "merge"]``,
    never the old three-stage ``governance_derivation`` shape, and no
    ``governance_derivation``-outcome log entry is emitted anywhere in this
    pipeline (structural: the stage itself was deleted outright in Slice 1).
    """
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions", json={"source": "internal", "content": _REAL_SEED_DOCUMENT}
    )

    assert response.status_code == 200
    body = response.json()
    assert [stage["stage"] for stage in body["stages"]] == ["internal_ingestion", "merge"]
    internal_ingestion_stage = body["stages"][0]
    assert "policies" in internal_ingestion_stage["summary"]
    assert "standards" in internal_ingestion_stage["summary"]
    assert "controls" in internal_ingestion_stage["summary"]
    assert "practice_areas" in internal_ingestion_stage["summary"]
    assert "risk_paths" in internal_ingestion_stage["summary"]


def test_second_ingestion_same_title_is_structural_no_op() -> None:
    """D4/AC-BI-018 (CHANGES.md; PLAN.md §6 S4 item 5): posting the exact
    same internal fixture TWICE in a row is safe -- both requests succeed,
    both report all three stages `succeeded`, and both runs resolve to the
    SAME ``regulatory_instrument_id`` and drive the ``merge`` stage with the
    SAME id both times. `RegulatoryInstrument`/`Policy` convergence by exact
    canonical id is what `company_merge`'s own `MERGE`/`ON CREATE SET`
    semantics guarantee at the graph-write level (proven directly in
    `tests/company_merge/test_merge_baseline_graph.py`/
    `test_merge_idempotency.py`, not re-proven here); this route-level test
    proves the orchestration itself imposes no obstacle to a byte-identical
    re-ingestion -- no crash, no special-cased second-request handling, no
    drift in which stages run or which id they run against.
    """
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    first = client.post("/ingestions", json={"source": "internal", "content": _REAL_SEED_DOCUMENT})
    second = client.post("/ingestions", json={"source": "internal", "content": _REAL_SEED_DOCUMENT})

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()

    assert first_body["regulatory_instrument_id"] == "ENGPRAC-3.0"
    assert second_body["regulatory_instrument_id"] == "ENGPRAC-3.0"
    for body in (first_body, second_body):
        assert [stage["stage"] for stage in body["stages"]] == ["internal_ingestion", "merge"]
        assert all(stage["status"] == "succeeded" for stage in body["stages"])

    # Both full pipeline runs happened, in the same order, each driving the
    # merge stage against the SAME regulatory_instrument_id both times.
    assert fake.recorder.order == [
        "internal_ingestion",
        "merge",
        "internal_ingestion",
        "merge",
    ]
    merge_calls = [call for call in fake.recorder.calls if call.stage == "merge"]
    assert len(merge_calls) == 2
    assert merge_calls[0].regulatory_instrument_id == "ENGPRAC-3.0"
    assert merge_calls[1].regulatory_instrument_id == "ENGPRAC-3.0"


def test_internal_ingestion_stage_failure_aborts_before_merge_and_names_stage() -> None:
    """AC-BI-013: an ``internal_ingestion`` stage failure 502s, names the
    stage, and no later stage (``merge``) ever runs. Replaces the deleted
    ``test_governance_stage_failure_aborts_before_merge_and_names_stage``
    (GH #76 removed the ``governance_derivation`` stage outright) -- this is
    the internal pipeline's remaining first-stage failure-path proof,
    mirroring
    ``test_ingestions_catalog.py::test_stage_error_response_reports_failing_stage_and_sanitized_reason``'s
    exact shape for the catalog pipeline.
    """
    fake = build_fake_pipeline_dependencies(
        internal_rid="ENGPRAC-3.0",
        ingest_internal_error=InternalSeedError("referential integrity violation"),
    )
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions", json={"source": "internal", "content": _REAL_SEED_DOCUMENT}
    )

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["failing_stage"] == "internal_ingestion"
    assert body["error"]["message"]
    assert fake.recorder.order == ["internal_ingestion"]
