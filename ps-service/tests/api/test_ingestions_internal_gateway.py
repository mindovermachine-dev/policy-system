"""``POST /ingestions`` (internal) writes the seed's instrument through the gateway.

The thin vertical path, end to end over the HTTP route: request ->
``run_internal_ingestion_pipeline`` -> system audit row -> the real ``internal_seed`` ingest stage
-> ``GroupSubmitter`` (the gateway rig) -> log -> graph -> read-back. Only the true boundaries are
fakes: the gateway's Postgres and FalkorDB (``GatewayRig``), the audit database (a recorder that
hands out ids) and the legacy graph handles the not-yet-migrated writes still use. The response
is compared with the one the route returned before the migration (the faked-stage reference), and
a deployment with no gateway fails closed without touching any graph.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from fastapi import Request
from fastapi.testclient import TestClient
from graph_gateway._fakes import GatewayRig

from api._fakes import FakePipeline, build_fake_pipeline_dependencies
from api._gateway_fakes import RecordingSystemAudit, override_gateway
from ps_service.api.dependencies import provide_graph_write_gateway, provide_pipeline_dependencies
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.identity import capability_id
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.ingestion.adapters.internal_seed.persist import (
    ingest_internal_regulatory_instrument,
)
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.usefixtures("grant_compliance_officer")

_SEED_NODES: list[dict[str, object]] = [
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
]
_SEED_DOCUMENT: dict[str, object] = {"nodes": _SEED_NODES, "edges": []}
_NATIVE_GRAPH = "testreg_native"
# `ps-graph-digest-v1` of a native graph that holds only the instrument above.
_NATIVE_DIGEST = "sha256:ffac18b7da459260b7c8850f5916c2fcd0f536b169d97a783c65b57b6a5272b0"


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
        company_merge_role_review_threshold=ROLE_REVIEW_THRESHOLD,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _real_ingest_stage(fake: FakePipeline) -> FakePipeline:
    """The fake pipeline with the real ``internal_seed`` ingest stage in place of its fake."""
    stages = dataclasses.replace(
        fake.dependencies.stages, ingest_internal=ingest_internal_regulatory_instrument
    )
    return dataclasses.replace(
        fake, dependencies=dataclasses.replace(fake.dependencies, stages=stages)
    )


def _client(
    fake: FakePipeline, rig: GatewayRig | None, system_audit: RecordingSystemAudit
) -> TestClient:
    app = create_app(_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    override_gateway(app, rig, system_audit)
    return TestClient(app, raise_server_exceptions=False)


def _post(client: TestClient) -> tuple[int, dict[str, Any]]:
    response = client.post("/ingestions", json={"source": "internal", "content": _SEED_DOCUMENT})
    return response.status_code, response.json()


def test_response_is_what_the_route_returned_before_the_migration(rig: GatewayRig) -> None:
    reference = build_fake_pipeline_dependencies(internal_rid="TESTREG-1.0")
    reference_status, reference_body = _post(_client(reference, rig, RecordingSystemAudit(rig)))
    migrated = _real_ingest_stage(build_fake_pipeline_dependencies())

    status, body = _post(_client(migrated, rig, RecordingSystemAudit(rig)))

    assert status == reference_status == 200
    assert {**body, "run_id": ""} == {**reference_body, "run_id": ""}
    assert body["regulatory_instrument_id"] == "TESTREG-1.0"
    assert body["source"] == "internal"
    assert body["outcome"] == "fresh"
    assert [stage["stage"] for stage in body["stages"]] == ["internal_ingestion", "merge"]


def test_instrument_is_logged_under_a_system_audit_row_and_read_back(rig: GatewayRig) -> None:
    system_audit = RecordingSystemAudit(rig)
    fake = _real_ingest_stage(build_fake_pipeline_dependencies())

    status, body = _post(_client(fake, rig, system_audit))

    assert status == 200
    (row,) = system_audit.rows
    assert (row["action"], row["resource_type"], row["resource_id"]) == (
        "ingestion_run.submit",
        "ingestion_run",
        body["run_id"],
    )
    assert [(group.graph, group.audit_event_id) for group in rig.store.groups] == [
        (_NATIVE_GRAPH, system_audit.ids[0]),
        ("testreg_baseline", system_audit.ids[0]),
    ]
    native = rig.graphs.open(_NATIVE_GRAPH)
    assert ("RegulatoryInstrument", "TESTREG-1.0") in native.nodes
    assert canonical_digest(native) == _NATIVE_DIGEST
    assert fake.native.calls == []  # the native graph does not go through a legacy handle


def test_post_onto_a_legacy_shaped_graph_adds_no_parallel_edge_and_a_second_post_logs_nothing(
    rig: GatewayRig,
) -> None:
    """F1 over HTTP: the route's submit path stamps the legacy edges before it writes."""
    seed_document: dict[str, object] = {
        "nodes": [
            *_SEED_NODES,
            {"label": "Role", "id": "role-a", "properties": {"name": "Role A"}},
        ],
        "edges": [
            {
                "type": "DEFINES",
                "from": {"label": "RegulatoryInstrument", "id": "TESTREG-1.0"},
                "to": {"label": "Role", "id": "role-a"},
                "properties": {"source_ref": "Art. 1"},
            }
        ],
    }
    legacy = rig.graphs.open(_NATIVE_GRAPH)
    legacy.seed_node(("RegulatoryInstrument",), "TESTREG-1.0", {})
    legacy.seed_node(("Role",), "role-a", {"name": "Role A"})
    legacy.seed_legacy_edge(
        ("DEFINES", "RegulatoryInstrument", "TESTREG-1.0", "Role", "role-a"),
        {"source_ref": "Art. 1"},
    )
    system_audit = RecordingSystemAudit(rig)
    client = _client(_real_ingest_stage(build_fake_pipeline_dependencies()), rig, system_audit)

    first = client.post("/ingestions", json={"source": "internal", "content": seed_document})

    assert first.status_code == 200, first.json()
    native = rig.graphs.open(_NATIVE_GRAPH)
    assert len(native.edges) == 1  # the legacy edge, stamped in place: no parallel edge
    ((*_, identity),) = native.edges
    assert identity == "DEFINES:TESTREG-1.0->role-a"
    assert {group.audit_event_id for group in rig.store.groups} == {system_audit.ids[0]}
    logged = len(rig.store.groups)

    second = client.post("/ingestions", json={"source": "internal", "content": seed_document})

    assert second.status_code == 200
    assert len(rig.store.groups) == logged  # nothing to stamp, nothing changed: nothing logged
    assert len(rig.graphs.open(_NATIVE_GRAPH).edges) == 1


def test_without_a_gateway_the_route_fails_closed_and_writes_to_no_graph(rig: GatewayRig) -> None:
    system_audit = RecordingSystemAudit(rig)
    fake = _real_ingest_stage(build_fake_pipeline_dependencies())

    status, body = _post(_client(fake, None, system_audit))

    assert status == 503
    assert body["error"]["code"] == "graph_write_unavailable"
    assert fake.native.calls == []
    assert fake.baseline.calls == []
    assert fake.recorder.order == []
    assert system_audit.rows == []
    assert rig.store.groups == []


def test_an_audit_outage_fails_closed_with_503_before_anything_is_written(
    rig: GatewayRig,
) -> None:
    system_audit = RecordingSystemAudit(rig, error=AuditTrailUnavailableError("down"))
    fake = _real_ingest_stage(build_fake_pipeline_dependencies())

    status, body = _post(_client(fake, rig, system_audit))

    assert status == 503
    assert body["error"]["code"] == "audit_trail_unavailable"
    assert fake.native.calls == []
    assert fake.baseline.calls == []
    assert rig.store.groups == []


def test_a_gateway_failure_is_the_same_502_a_graph_write_failure_always_was(
    rig: GatewayRig,
) -> None:
    rig.store.fail_appends()
    fake = _real_ingest_stage(build_fake_pipeline_dependencies())

    status, body = _post(_client(fake, rig, RecordingSystemAudit(rig)))

    assert status == 502
    assert body["error"]["failing_stage"] == "internal_ingestion"
    assert "FalkorDB write failed" in body["error"]["message"]
    assert fake.native.calls == []
    assert fake.baseline.calls == []


def test_provider_returns_the_gateway_the_app_started_with_and_none_when_it_has_none(
    rig: GatewayRig,
) -> None:
    app = create_app(_config())
    request = Request({"type": "http", "app": app, "headers": []})

    assert provide_graph_write_gateway(request) is None  # no state Postgres: no gateway built

    app.state.graph_write_gateway = rig.gateway

    assert provide_graph_write_gateway(request) is rig.gateway


def _document_with_capability(capability_id: str, name: str) -> dict[str, object]:
    return {
        "nodes": [
            *_SEED_NODES,
            {"label": "Capability", "id": capability_id, "properties": {"name": name}},
        ],
        "edges": [],
    }


def test_renaming_a_capability_removes_the_old_node_from_both_graphs(rig: GatewayRig) -> None:
    """Over HTTP, a resubmission that renames a Capability retires the old node."""
    client = _client(
        _real_ingest_stage(build_fake_pipeline_dependencies()), rig, RecordingSystemAudit(rig)
    )
    old_id, new_id = capability_id("Cap One"), capability_id("Cap Two")
    first = client.post(
        "/ingestions",
        json={"source": "internal", "content": _document_with_capability("cap-1", "Cap One")},
    )
    assert first.status_code == 200, first.json()
    assert ("Capability", old_id) in rig.graphs.open("testreg_baseline").nodes

    second = client.post(
        "/ingestions",
        json={"source": "internal", "content": _document_with_capability("cap-2", "Cap Two")},
    )

    assert second.status_code == 200, second.json()
    native, baseline = rig.graphs.open(_NATIVE_GRAPH), rig.graphs.open("testreg_baseline")
    assert ("Capability", "cap-1") not in native.nodes
    assert ("Capability", "cap-2") in native.nodes
    assert ("Capability", old_id) not in baseline.nodes
    assert ("Capability", new_id) in baseline.nodes
