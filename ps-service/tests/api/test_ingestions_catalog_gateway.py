"""``POST /ingestions`` (catalog) writes the native graph through the gateway.

The route's ingest stage is handed the process's Graph Write Gateway and the id of a system audit
row recorded for the run; with no gateway the run fails closed before any stage. The stages
themselves are the standard fake pipeline: what is proved here is the hand-off and the contract
of the response, which is the one the route returned before the migration.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from litellm.types.utils import Choices, Message, ModelResponse

from api._audit_fakes import InMemoryAuditStore
from api._fakes import build_fake_pipeline_dependencies, install_compliance_officer_grant
from ps_service.api.dependencies import provide_audit_store, provide_pipeline_dependencies
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.extraction import extract_roles_and_requirements
from ps_service.domain_mapper.models import ExtractionUnit
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.ingestion.models import IngestResult
from ps_service.main import create_app

if TYPE_CHECKING:
    from domain_mapper._fakes import MakeEmitter

    from api._fakes import FakePipeline
    from api._gateway_fakes import ProcessGateway
    from ps_service.api.ingestion_orchestration import ExtractStage
    from ps_service.domain_mapper.models import ExtractionResult
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import CompletionCaller
    from ps_service.logging import LogEmitter

_BODY = {"source": "catalog", "celex": "32024R2847", "short_name": "cra"}


def _noop_emit(**_kwargs: object) -> None:
    """Discard a run-log entry (Logging boundary stub)."""


@pytest.fixture(autouse=True)
def _stub_run_log(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    # detroit-exception: process-wide atexit logging facade, not a business fake
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


# --- ingest then extract: one run, one audit event ---


_UNIT = ExtractionUnit(
    citation_ref="Art. 13(1)",
    text="The manufacturer shall conduct a cybersecurity risk assessment.",
    article_number="13",
    paragraph_number="1",
    article_heading="Obligations of manufacturers",
)


class _OneUnitAdapter:
    """The Domain Mapping adapter boundary: one fixed unit, no native-graph parse."""

    def read_native_units(
        self, graph: object, regulatory_instrument_id: str
    ) -> tuple[ExtractionUnit, ...]:
        _ = (graph, regulatory_instrument_id)
        return (_UNIT,)


class _NativeInstrumentNode:
    def __init__(self) -> None:
        self.properties: dict[str, object] = {"id": "cra-1.0", "title": "Cyber Resilience Act"}


class _NativeInstrumentResult:
    @property
    def result_set(self) -> list[object]:
        return [[_NativeInstrumentNode()]]


class _NativeInstrumentGraph:
    """The native graph handle, answering the one read extraction makes of it."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _NativeInstrumentResult:
        _ = (q, params)
        return _NativeInstrumentResult()


def _one_requirement(
    *, model: str, messages: list[dict[str, str]], timeout: float
) -> ModelResponse:
    """The LLM boundary: one Manufacturer requirement for the one unit."""
    _ = (model, messages, timeout)
    content = json.dumps(
        {
            "requirements": [
                {
                    "role_name": "Manufacturer",
                    "text": "Conduct a cybersecurity risk assessment.",
                    "type": "requirement",
                    "letter_suffix": None,
                    "confidence": 0.9,
                }
            ]
        }
    )
    return ModelResponse(
        id="x",
        model="fake-model",
        choices=[
            Choices(
                finish_reason="stop", index=0, message=Message(content=content, role="assistant")
            )
        ],
    )


def _ingest_through_the_gateway(
    identifier: str,
    short_name: str,
    *,
    version: str,
    adapter: object,
    graph: object,
    submitter: GroupSubmitter,
    audit_event_id: str,
    run_id: str | None = None,
    emitter: object = None,
) -> IngestResult:
    """An ingest stage that writes its native group the way the real one does: via the gateway."""
    _ = (identifier, version, adapter, graph, emitter)
    submitter.submit_group(
        MutationGroup(
            graph=f"{short_name.lower()}_native",
            audit_event_id=audit_event_id,
            primitives=(
                UpsertNode(
                    label="RegulatoryInstrument",
                    id="CRA-1.0",
                    properties={"title": "Cyber Resilience Act"},
                ),
            ),
        )
    )
    return IngestResult(regulatory_instrument_id="CRA-1.0", run_id=run_id or "run", counts={})


def _extract_for_real(run_log: LogEmitter) -> ExtractStage:
    """The real extraction stage over the process gateway; adapter, native read and LLM are fakes.

    The run's log sink is the test's own emitter: the route's run log is stubbed above, so the
    orchestrator hands the stage none.
    """

    def stage(
        regulatory_instrument_id: str,
        *,
        adapter: object,
        native_graph: object,
        submitter: GroupSubmitter,
        baseline_graph_name: str,
        audit_event_id: str,
        model: str,
        call_completion: CompletionCaller | None = None,
        emitter: object = None,
    ) -> ExtractionResult:
        _ = (adapter, native_graph, call_completion, emitter)
        return extract_roles_and_requirements(
            regulatory_instrument_id,
            adapter=_OneUnitAdapter(),
            native_graph=_NativeInstrumentGraph(),  # pyright: ignore[reportArgumentType] -- structural GraphHandle
            submitter=submitter,
            baseline_graph_name=baseline_graph_name,
            audit_event_id=audit_event_id,
            model=model,
            call_completion=_one_requirement,
            emitter=run_log,
        )

    return stage  # pyright: ignore[reportReturnType] -- structural ExtractStage


def test_catalog_run_logs_the_native_group_and_the_extraction_group_under_the_run_audit_event(
    process_gateway: ProcessGateway, make_emitter: MakeEmitter
) -> None:
    run_log, _log_path = make_emitter()
    fake = build_fake_pipeline_dependencies()
    stages = replace(
        fake.dependencies.stages,
        ingest=_ingest_through_the_gateway,  # pyright: ignore[reportArgumentType] -- structural IngestStage
        extract=_extract_for_real(run_log),
    )
    fake = replace(fake, dependencies=replace(fake.dependencies, stages=stages))

    response = _client(fake).post("/ingestions", json=_BODY)

    assert response.status_code == 200, response.text
    [audit_event_id] = process_gateway.system_audit.ids
    groups = process_gateway.rig.store.groups
    assert [(group.graph, group.audit_event_id) for group in groups] == [
        ("cra_native", audit_event_id),
        ("cra_baseline", audit_event_id),
    ]
    baseline_group = groups[1]
    assert [entry.name for entry in baseline_group.entries] == [
        "RegulatoryInstrument",
        "Role",
        "DEFINES",
        "Requirement",
        "EXPRESSES",
    ]
    baseline = process_gateway.rig.graphs.open("cra_baseline")
    assert sorted(label for label, _node_id in baseline.nodes) == [
        "RegulatoryInstrument",
        "Requirement",
        "Role",
    ]
    assert len(baseline.edges) == 2
    assert [stage["stage"] for stage in response.json()["stages"]] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]
