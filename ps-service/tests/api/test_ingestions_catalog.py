"""HTTP tests for ``POST /ingestions`` -- the catalog (CELEX) ingestion path.

Increment 6 (#51a). Drives the route with ``TestClient`` and an
``app.dependency_overrides`` fake ``PipelineDependencies`` (``tests/api/_fakes.py``)
so request validation, catalog lookup, the pipeline hand-off, and error mapping
are exercised without a real graph, adapter, or LLM. The well-formed
``source: "internal"`` request is covered here too: it must validate and then
return a clean 501 referencing issue #54 (the internal pipeline is #54).

AC coverage: AC-BI-002 (runs the pipeline, reports stages), AC-BI-006 (unknown
CELEX -> 404, malformed body -> 422, both before any stage), AC-BI-008/009
(stage failure -> 502 naming the stage, message free of paths / host:port),
AC-BI-010 (run id in the body).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from api._fakes import build_fake_pipeline_dependencies
from ps_service.api.dependencies import provide_pipeline_dependencies
from ps_service.api.ingestion_orchestration import (
    _derive_short_name,  # pyright: ignore[reportPrivateUsage] — internal helper under test
)
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.ingestion.adapters.errors import CellarFetchError, CellarNotFoundError
from ps_service.main import create_app

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import PipelineDependencies

_VALID_CELEX = "32024R2847"
_UNKNOWN_CELEX = "32099R9999"
_HOST_PORT_RE = re.compile(r"\b[\w.\-]+:\d{2,5}\b")

# A CELEX absent from the curated catalog but well-formed and (per this
# fixture) resolvable via Cellar/ELI -- Increment 8's fallback path.
_NONCURATED_CELEX = "32020R1111"
_NONCURATED_TITLE = "Regulation (EU) 1111/1111 Fixture A"

# A Regulation-shaped Cellar XHTML fixture, mirroring
# tests/api/test_ingestion_orchestration.py's `_FIXTURE_REGULATION_A`.
_FIXTURE_REGULATION_A = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Regulation (EU) 1111/1111 Fixture A</div>
<div class="eli-subdivision" id="cpt_I">
<div class="eli-title" id="cpt_I.tit_1">CHAPTER I General provisions</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Entry into force and application</div>
<div>This Regulation shall enter into force on the twentieth day following
publication. It shall apply from 1 January 2030.</div>
</div>
</div>
</div>
</body>
</html>
"""

# RDF own-subject fixture for `_NONCURATED_CELEX` -- own subject asserts
# both `resource_legal_id_celex` and `date_entry-into-force`, so
# `extract_metadata` resolves `effective_date` successfully. Mirrors
# `tests/api/test_ingestion_orchestration.py`'s `_RDF_FIXTURE_REGULATION_A`.
_RDF_FIXTURE_REGULATION_A = b"""<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:j.0="http://publications.europa.eu/ontology/cdm#">
<rdf:Description rdf:about="http://publications.europa.eu/resource/celex/32020R1111">
<j.0:resource_legal_id_celex rdf:datatype="http://www.w3.org/2001/XMLSchema#string">32020R1111</j.0:resource_legal_id_celex>
<j.0:date_entry-into-force rdf:datatype="http://www.w3.org/2001/XMLSchema#date">2030-01-01</j.0:date_entry-into-force>
</rdf:Description>
</rdf:RDF>
"""


def _noop_emit(**_kwargs: object) -> None:
    """Discard a run-log entry (Logging boundary stub -- see ``_stub_run_log``)."""


@pytest.fixture(autouse=True)
def _stub_run_log(  # pyright: ignore[reportUnusedFunction]  # module autouse fixture — invoked by name-collection
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the Logging boundary so the pipeline needs no ``configure()``d facade.

    ``run_catalog_ingestion_pipeline`` emits its ``ingestion_run`` entries through
    the process-wide default emitter. These fast HTTP tests deliberately do not
    ``configure()`` that global (a dedicated ``tests/logging`` assertion owns its
    once-only ``atexit`` registration); the run-log *content* is already covered
    against a real emitter in ``test_ingestion_orchestration.py``. Increment 7's
    ``test_run_context.py`` exercises the real facade through the HTTP layer.
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


def test_valid_celex_runs_pipeline_and_returns_run_id_and_stages() -> None:
    """AC-BI-002/010: a known CELEX runs all four stages and the body carries run id + stages."""
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["regulatory_instrument_id"] == "CRA-1.0"
    assert body["source"] == "catalog"
    assert [stage["stage"] for stage in body["stages"]] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]


def test_ingestion_accepted_response_has_exactly_the_documented_fields() -> None:
    """AC-BI-011 regression proof: adding ``GET /ingestions/{run_id}`` (Increment 12)
    does not add or remove a field from ``IngestionAcceptedResponse``'s wire shape.
    """
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"run_id", "regulatory_instrument_id", "source", "stages"}


def test_unknown_celex_returns_404_structured_and_starts_no_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005/006: a CELEX absent from both the curated catalog and Cellar/ELI 404s
    before any stage runs.

    ``_UNKNOWN_CELEX`` is well-formed, so the Cellar-fallback path (Increment 8) engages
    after the catalog miss; ``fetch_xhtml`` is monkeypatched to raise ``CellarNotFoundError``
    so this stays a genuine not-found on both sources and makes no real network call.
    """

    def _not_found_fetch(celex: str) -> bytes:
        raise CellarNotFoundError(f"CELEX {celex!r} was not found on Cellar/ELI")

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _not_found_fetch)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _UNKNOWN_CELEX})

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"]
    assert body["error"]["message"]
    assert "run_id" in body
    assert fake.recorder.order == []


def test_malformed_body_returns_422_structured_and_starts_no_pipeline() -> None:
    """AC-BI-006: a body that fails Pydantic validation 422s before any stage runs."""
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"]
    assert "run_id" in body
    assert fake.recorder.order == []


def test_stage_error_response_reports_failing_stage_and_sanitized_reason() -> None:
    """AC-BI-008/009: a stage failure 502s, names the stage, and leaks no path / host:port."""
    fake = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError("no candidates for requirement unit 7"),
    )
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["failing_stage"] == "extraction"
    message = body["error"]["message"]
    assert message
    assert "://" not in message
    assert _HOST_PORT_RE.search(message) is None
    assert fake.recorder.order == ["ingestion", "extraction"]


def test_create_ingestion_echoes_client_supplied_run_id_in_response() -> None:
    """AC-BI-008 correlation: a client-supplied ``run_id`` is used as the run's
    effective correlation id and echoed in the accepted response.
    """
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "run_id": "client-abc123"},
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "client-abc123"


def test_create_ingestion_auto_mints_run_id_when_client_omits_it() -> None:
    """Regression: a request with no ``run_id`` field still gets a fresh server-minted one."""
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 200
    assert response.json()["run_id"]


def test_internal_request_returns_501_referencing_54() -> None:
    """A well-formed ``source: "internal"`` request validates, then 501s naming issue #54."""
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": "internal",
            "fixture_path": "engineering-practices/engineering-practices-seed.json",
        },
    )

    assert response.status_code == 501
    body = response.json()
    assert "#54" in body["error"]["message"]
    assert fake.recorder.order == []


# --- Cellar fallback, end-to-end over HTTP (Increment 8, AC-BI-003/004/005/006/007) --


def test_non_curated_celex_resolves_via_cellar_and_runs_full_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-003/004: a well-formed, non-curated CELEX resolves via Cellar/ELI and runs
    all four pipeline stages, with the derived short_name/version reaching the ingest
    stage.
    """

    def _fetch(celex: str) -> bytes:
        _ = celex
        return _FIXTURE_REGULATION_A

    def _fetch_rdf(celex: str) -> bytes:
        _ = celex
        return _RDF_FIXTURE_REGULATION_A

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _fetch)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_rdf", _fetch_rdf)
    expected_short_name = _derive_short_name(_NONCURATED_TITLE, _NONCURATED_CELEX)
    fake = build_fake_pipeline_dependencies(rid=f"{expected_short_name}-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _NONCURATED_CELEX})

    assert response.status_code == 200
    body = response.json()
    assert body["regulatory_instrument_id"] == f"{expected_short_name}-1.0"
    assert [stage["stage"] for stage in body["stages"]] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]
    assert fake.recorder.calls[0].kwargs["short_name"] == expected_short_name
    assert fake.recorder.calls[0].kwargs["version"] == "1.0"


def test_non_curated_celex_not_found_on_cellar_returns_404_before_any_stage_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: a non-curated CELEX that Cellar/ELI also doesn't recognise 404s before
    any stage runs.
    """

    def _not_found_fetch(celex: str) -> bytes:
        raise CellarNotFoundError(f"CELEX {celex!r} was not found on Cellar/ELI")

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _not_found_fetch)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _NONCURATED_CELEX})

    assert response.status_code == 404
    assert fake.recorder.order == []


def test_non_curated_celex_cellar_unreachable_returns_502_naming_ingestion_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-007: a Cellar/ELI outage (distinct from not-found) surfaces as a 502 naming
    the ingestion stage, before any pipeline stage runs.
    """

    def _failing_fetch(celex: str) -> bytes:
        raise CellarFetchError(f"Cellar/ELI fetch failed for CELEX {celex!r}")

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _failing_fetch)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _NONCURATED_CELEX})

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["failing_stage"] == "ingestion"
    assert fake.recorder.order == []


def test_non_curated_celex_document_fetched_exactly_once_for_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-006: each Cellar/ELI document (XHTML, RDF) is fetched exactly once for
    the whole HTTP round trip -- once during resolution, never again by the ingest
    stage. Direct end-to-end proof, over HTTP, of the "fetch-once, replay both
    cached payloads" regression guard (PLAN_REVISED.md §6 item 2) -- without the
    RDF-side replay, the ingest stage would issue a third real Cellar/ELI request.
    """
    xhtml_call_count = 0
    rdf_call_count = 0

    def _counting_fetch(celex: str) -> bytes:
        nonlocal xhtml_call_count
        xhtml_call_count += 1
        _ = celex
        return _FIXTURE_REGULATION_A

    def _counting_fetch_rdf(celex: str) -> bytes:
        nonlocal rdf_call_count
        rdf_call_count += 1
        _ = celex
        return _RDF_FIXTURE_REGULATION_A

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _counting_fetch)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_rdf", _counting_fetch_rdf)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _NONCURATED_CELEX})

    assert response.status_code == 200
    assert xhtml_call_count == 1
    assert rdf_call_count == 1


def test_curated_celex_unaffected_by_cellar_fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a curated CELEX still runs the pipeline via the catalog fast path,
    with the Cellar-fallback ``fetch_xhtml`` installed but never invoked.
    """

    def _unreachable_fetch(celex: str) -> bytes:
        message = f"fetch_xhtml must not be called for a curated CELEX {celex!r}"
        raise AssertionError(message)

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _unreachable_fetch)
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 200
    body = response.json()
    assert body["regulatory_instrument_id"] == "CRA-1.0"
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]
