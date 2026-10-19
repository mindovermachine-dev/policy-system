"""A rejected pre-flight returns today's error and logs nothing to the graph.

The three pre-flights of the catalog path (a CELEX already in the graph, a short name claimed by
another CELEX, an instrument already fully merged) run before any group is built. Each is driven
through `POST /ingestions` with the process's gateway rig installed: the response is the one the
route always returned, and the rig holds no group, the system audit recorder no row, and the
run log no `graph_gateway` entry. The internal route's seed-shape rejection is held to the same
standard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD
from fastapi.testclient import TestClient

from api._audit_fakes import InMemoryAuditStore
from api._fakes import build_fake_pipeline_dependencies, install_compliance_officer_grant
from api._gateway_fakes import override_gateway
from ps_service.api.dependencies import provide_audit_store, provide_pipeline_dependencies
from ps_service.config import ServiceConfig
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from company_merge._fakes import ReadLines

    from api._fakes import FakePipeline
    from api._gateway_fakes import ProcessGateway

_BODY = {"source": "catalog", "celex": "32024R2847", "short_name": "cra"}


@pytest.fixture(autouse=True)
def _grant(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]  # autouse
    install_compliance_officer_grant(monkeypatch, granted=True)


def _client(fake: FakePipeline, process_gateway: ProcessGateway | None = None) -> TestClient:
    config = ServiceConfig(
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
    app = create_app(config)
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake.dependencies
    store = InMemoryAuditStore()
    app.dependency_overrides[provide_audit_store] = lambda: store
    if process_gateway is not None:
        override_gateway(app, process_gateway.rig, process_gateway.system_audit)
    return TestClient(app, raise_server_exceptions=False)


def _assert_nothing_reached_the_graph_log(
    process_gateway: ProcessGateway, configured_logging: Path, read_lines: ReadLines
) -> None:
    rig = process_gateway.rig
    assert rig.store.groups == []
    assert rig.store.entries == {}
    assert rig.store.events == []
    assert process_gateway.system_audit.rows == []
    facade.reset_for_tests()  # drains the writer so the file can be read
    components = {line.get("component") for line in read_lines(configured_logging)}
    assert "graph_gateway" not in components


def test_a_celex_already_in_the_graph_is_the_same_409_and_nothing_is_logged(
    process_gateway: ProcessGateway, configured_logging: Path, read_lines: ReadLines
) -> None:
    fake = build_fake_pipeline_dependencies(celex_row="cra-1.0")

    response = _client(fake).post("/ingestions", json=_BODY)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "celex_already_ingested"
    assert fake.recorder.order == []
    _assert_nothing_reached_the_graph_log(process_gateway, configured_logging, read_lines)


def test_a_short_name_claimed_by_another_celex_is_the_same_409_and_nothing_is_logged(
    process_gateway: ProcessGateway, configured_logging: Path, read_lines: ReadLines
) -> None:
    fake = build_fake_pipeline_dependencies(collision_row=("CRA-1.0", "32000R0001"))

    response = _client(fake).post("/ingestions", json=_BODY)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "short_name_collision"
    assert fake.recorder.order == []
    _assert_nothing_reached_the_graph_log(process_gateway, configured_logging, read_lines)


def test_an_instrument_already_merged_is_the_same_200_and_nothing_is_logged(
    process_gateway: ProcessGateway, configured_logging: Path, read_lines: ReadLines
) -> None:
    fake = build_fake_pipeline_dependencies(preflight_hit=True)

    response = _client(fake).post("/ingestions", json=_BODY)

    assert response.status_code == 200
    body = response.json()
    assert (body["outcome"], body["stages"]) == ("already_ingested", [])
    assert fake.recorder.order == []
    _assert_nothing_reached_the_graph_log(process_gateway, configured_logging, read_lines)


def test_a_pre_flight_that_cannot_read_the_graph_is_the_same_502_and_nothing_is_logged(
    process_gateway: ProcessGateway, configured_logging: Path, read_lines: ReadLines
) -> None:
    fake = build_fake_pipeline_dependencies(preflight_error=RuntimeError("graph down"))

    response = _client(fake).post("/ingestions", json=_BODY)

    assert response.status_code == 502
    assert fake.recorder.order == []
    _assert_nothing_reached_the_graph_log(process_gateway, configured_logging, read_lines)


def test_an_internal_seed_that_fails_shape_validation_is_the_same_422_and_nothing_is_logged(
    process_gateway: ProcessGateway, configured_logging: Path, read_lines: ReadLines
) -> None:
    fake = build_fake_pipeline_dependencies()

    response = _client(fake, process_gateway).post(
        "/ingestions", json={"source": "internal", "content": {"nodes": "not-a-list"}}
    )

    assert response.status_code == 422
    _assert_nothing_reached_the_graph_log(process_gateway, configured_logging, read_lines)
