"""HTTP tests for ``POST /ingestions`` -- the catalog (CELEX) ingestion path.

Increment 6 (#51a). Drives the route with ``TestClient`` and an
``app.dependency_overrides`` fake ``PipelineDependencies`` (``tests/api/_fakes.py``)
so request validation, catalog lookup, the pipeline hand-off, and error mapping
are exercised without a real graph, adapter, or LLM. The ``source: "internal"``
path is covered separately, in ``tests/api/test_ingestions_internal.py``
(issue #54).

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

from api._fakes import (
    build_fake_pipeline_dependencies,
    install_compliance_officer_grant,
    install_no_principal,
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.dependencies import provide_pipeline_dependencies
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.ingestion.adapters.errors import CellarFetchError, CellarNotFoundError
from ps_service.main import create_app

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import PipelineDependencies

_VALID_CELEX = "32024R2847"
_VALID_SHORT_NAME = "cra"  # curated-content/catalog.json's own short_name for _VALID_CELEX
_UNKNOWN_CELEX = "32099R9999"
_UNKNOWN_SHORT_NAME = "unknown"
_HOST_PORT_RE = re.compile(r"\b[\w.\-]+:\d{2,5}\b")

# A CELEX absent from the curated catalog but well-formed and (per this
# fixture) resolvable via Cellar/ELI -- Increment 8's fallback path.
_NONCURATED_CELEX = "32020R1111"
_NONCURATED_SHORT_NAME = "fixture-a"
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
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _client_with_fake(fake_deps: PipelineDependencies) -> TestClient:
    """A ``TestClient`` whose ``provide_pipeline_dependencies`` yields ``fake_deps``."""
    app = create_app(_app_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake_deps
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _grant_compliance_officer(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test below drives a caller holding `ComplianceOfficer` by default (issue #145).

    The ``source: "catalog"`` path of `POST /ingestions` is now gated behind
    `require_access_role` (inline, only on this non-internal branch --
    `test_ingestions_internal.py` needs no such fixture and is entirely
    untouched by this change). `require_access_role` has no local-test-bypass
    carve-out (PLAN.md §3.2). The dedicated denial-proof tests below
    re-monkeypatch this away for their own scenario (the same `monkeypatch`
    fixture instance, so the later call simply wins).
    """
    install_compliance_officer_grant(monkeypatch, granted=True)


def test_valid_celex_runs_pipeline_and_returns_run_id_and_stages() -> None:
    """AC-BI-002/010: a known CELEX runs all four stages and the body carries run id + stages."""
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

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


def test_valid_celex_already_merged_skips_pipeline_and_reports_already_ingested() -> None:
    """Issue #135, AC-BI-002/003: a CELEX with an existing fully-merged
    ``RegulatoryInstrument`` runs no stage at all and the body reports
    ``outcome="already_ingested"`` with an empty ``stages`` list.
    """
    fake = build_fake_pipeline_dependencies(preflight_hit=True)
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "catalog"
    assert body["outcome"] == "already_ingested"
    assert body["stages"] == []
    assert fake.recorder.order == []


def test_reingesting_same_celex_with_same_short_name_after_short_name_parity_lands_still_reports_already_ingested() -> (  # noqa: E501 - name mirrors CHANGES.md/PLAN.md scenario naming verbatim
    None
):
    """Issue #146 AC-BI-008: re-submitting the SAME ``(celex, short_name)`` pair
    after the short_name-parity/collision validation landed still reports
    ``already_ingested`` and runs no pipeline stage. The new collision check
    must not trip against the CELEX's own already-recorded
    ``RegulatoryInstrument`` row -- exactly why ``check_short_name_collision``'s
    query excludes ``n.celex == $celex`` (Slice 5a's
    ``test_check_short_name_collision_returns_none_when_the_only_matching_row_
    is_the_same_celex``). Proves the existing idempotent-re-ingestion path
    (issue #135's ``_is_already_merged`` preflight) is unaffected by the new
    validation now running ahead of it.
    """
    fake = build_fake_pipeline_dependencies(preflight_hit=True)
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "already_ingested"
    assert body["stages"] == []
    assert fake.recorder.order == []


def test_ingestion_accepted_response_has_exactly_the_documented_fields() -> None:
    """AC-BI-011 regression proof: adding ``GET /ingestions/{run_id}`` (Increment 12)
    and the ``outcome`` field (issue #135) does not otherwise add or remove a
    field from ``IngestionAcceptedResponse``'s wire shape.
    """
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "run_id",
        "regulatory_instrument_id",
        "source",
        "outcome",
        "stages",
    }


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

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _UNKNOWN_CELEX, "short_name": _UNKNOWN_SHORT_NAME},
    )

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


def test_missing_short_name_returns_422_structured_and_starts_no_pipeline() -> None:
    """AC-BI-001: a catalog-source body omitting the now-required ``short_name`` 422s
    before any stage runs -- the vertical proof that ``create_ingestion`` never runs
    (no pipeline dependency touched).
    """
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "catalog", "celex": _VALID_CELEX})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"]
    assert "run_id" in body
    assert fake.recorder.order == []


def test_curated_celex_mismatched_short_name_is_rejected_before_the_pipeline_runs() -> None:
    """Issue #146 AC-BI-004: a curated CELEX's request ``short_name`` that doesn't
    match the catalog's own value is rejected with 409, before any pipeline stage
    runs -- REST parity with the ``ingest_regulation`` MCP tool's own mismatch check.
    """
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _VALID_CELEX,
            "short_name": "not-the-real-short-name",
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "short_name_curated_mismatch"
    assert body["error"]["message"] == (
        f"CELEX {_VALID_CELEX} is curated under short_name '{_VALID_SHORT_NAME}'; "
        "pass that value, not 'not-the-real-short-name'"
    )
    assert "run_id" in body
    assert fake.recorder.order == []


def test_short_name_collision_with_an_already_ingested_different_celex_is_rejected_before_any_pipeline_graph_is_opened() -> (  # noqa: E501 - name mirrors CHANGES.md Appendix B verbatim
    None
):
    """Issue #146 AC-BI-006: a non-curated CELEX whose ``short_name`` is already
    recorded in the single-tenant graph under a *different* CELEX is rejected with
    409, before any pipeline stage runs -- REST parity with the ``ingest_regulation``
    MCP tool's own graph-side collision check.
    """
    fake = build_fake_pipeline_dependencies(
        collision_row=(f"{_NONCURATED_SHORT_NAME}-1.0", "32024R0001")
    )
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _NONCURATED_CELEX,
            "short_name": _NONCURATED_SHORT_NAME,
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "short_name_collision"
    assert body["error"]["message"] == (
        f"short_name '{_NONCURATED_SHORT_NAME}' is already claimed by CELEX 32024R0001"
    )
    assert "run_id" in body
    assert fake.recorder.order == []


def test_short_name_collision_with_a_curated_catalog_entry_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-006: a curated CELEX whose own ``short_name`` is already
    claimed by a *different* curated entry is rejected with 409, before any
    pipeline stage runs -- the catalog-side collision check.
    """
    fixture = (
        CatalogEntry("32024R0001", "Fixture One", "shared-name", "1.0"),
        CatalogEntry("32024R0002", "Fixture Two", "shared-name", "1.0"),
    )
    monkeypatch.setattr("ps_service.api.catalog.REGULATION_CATALOG", fixture)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": "32024R0001", "short_name": "shared-name"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "short_name_collision"
    assert body["error"]["message"] == (
        "short_name 'shared-name' is already claimed by CELEX 32024R0002"
    )
    assert "run_id" in body
    assert fake.recorder.order == []


def test_collision_check_graph_unreachable_fails_closed_into_502_before_any_pipeline_stage_runs() -> (  # noqa: E501 - name mirrors the sibling collision tests' verbatim-scenario naming
    None
):
    """Issue #146 AC-BI-007: an I/O failure in the graph-side collision check
    itself (the query, not the graph open) fails closed into the same 502
    shape every other pre-pipeline stage failure uses -- naming the
    ``collision_check`` stage -- rather than silently treating the failure as
    "no collision", and no pipeline stage ever runs.
    """
    fake = build_fake_pipeline_dependencies(
        collision_error=RuntimeError("boom -- must never reach the caller")
    )
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _NONCURATED_CELEX,
            "short_name": _NONCURATED_SHORT_NAME,
        },
    )

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["failing_stage"] == "collision_check"
    assert "run_id" in body
    assert fake.recorder.order == []


def test_stage_error_response_reports_failing_stage_and_sanitized_reason() -> None:
    """AC-BI-008/009: a stage failure 502s, names the stage, and leaks no path / host:port."""
    fake = build_fake_pipeline_dependencies(
        extract_error=DomainMapperExtractionError("no candidates for requirement unit 7"),
    )
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

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
        json={
            "source": "catalog",
            "celex": _VALID_CELEX,
            "short_name": _VALID_SHORT_NAME,
            "run_id": "client-abc123",
        },
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "client-abc123"


def test_create_ingestion_auto_mints_run_id_when_client_omits_it() -> None:
    """Regression: a request with no ``run_id`` field still gets a fresh server-minted one."""
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 200
    assert response.json()["run_id"]


# --- Cellar fallback, end-to-end over HTTP (Increment 8, AC-BI-003/004/005/006/007) --


def test_non_curated_celex_resolves_via_cellar_and_runs_full_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-002/003/004 (issue #146): a well-formed, non-curated CELEX resolves via
    Cellar/ELI and runs all four pipeline stages, with the caller-supplied
    ``short_name`` -- used verbatim, never derived from the fetched title -- reaching
    the ingest stage.
    """

    def _fetch(celex: str) -> bytes:
        _ = celex
        return _FIXTURE_REGULATION_A

    def _fetch_rdf(celex: str) -> bytes:
        _ = celex
        return _RDF_FIXTURE_REGULATION_A

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _fetch)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_rdf", _fetch_rdf)
    fake = build_fake_pipeline_dependencies(rid=f"{_NONCURATED_SHORT_NAME}-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _NONCURATED_CELEX,
            "short_name": _NONCURATED_SHORT_NAME,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["regulatory_instrument_id"] == f"{_NONCURATED_SHORT_NAME}-1.0"
    assert [stage["stage"] for stage in body["stages"]] == [
        "ingestion",
        "extraction",
        "derivation",
        "merge",
    ]
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]
    assert fake.recorder.calls[0].kwargs["short_name"] == _NONCURATED_SHORT_NAME
    assert fake.recorder.calls[0].kwargs["version"] == "1.0"


def test_non_curated_celex_uses_caller_supplied_short_name_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #146 AC-BI-002: the resulting id is exactly ``{short_name}-{version}``,
    never a title-derived slug, for a non-curated CELEX resolved via Cellar/ELI.
    """

    def _fetch(celex: str) -> bytes:
        _ = celex
        return _FIXTURE_REGULATION_A

    def _fetch_rdf(celex: str) -> bytes:
        _ = celex
        return _RDF_FIXTURE_REGULATION_A

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _fetch)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_rdf", _fetch_rdf)
    fake = build_fake_pipeline_dependencies(rid=f"{_NONCURATED_SHORT_NAME}-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _NONCURATED_CELEX,
            "short_name": _NONCURATED_SHORT_NAME,
        },
    )

    assert response.status_code == 200
    assert response.json()["regulatory_instrument_id"] == f"{_NONCURATED_SHORT_NAME}-1.0"


def test_non_curated_celex_never_calls_derive_short_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #146 AC-BI-003: ``_derive_short_name`` is never called once the request
    supplies its own ``short_name`` for the catalog path's Cellar-fallback.
    """

    def _fetch(celex: str) -> bytes:
        _ = celex
        return _FIXTURE_REGULATION_A

    def _fetch_rdf(celex: str) -> bytes:
        _ = celex
        return _RDF_FIXTURE_REGULATION_A

    def _explode(title: str, celex: str) -> str:
        message = f"_derive_short_name must not be called (title={title!r}, celex={celex!r})"
        raise AssertionError(message)

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _fetch)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_rdf", _fetch_rdf)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration._derive_short_name", _explode)
    fake = build_fake_pipeline_dependencies(rid=f"{_NONCURATED_SHORT_NAME}-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _NONCURATED_CELEX,
            "short_name": _NONCURATED_SHORT_NAME,
        },
    )

    assert response.status_code == 200


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

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _NONCURATED_CELEX,
            "short_name": _NONCURATED_SHORT_NAME,
        },
    )

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

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _NONCURATED_CELEX,
            "short_name": _NONCURATED_SHORT_NAME,
        },
    )

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

    response = client.post(
        "/ingestions",
        json={
            "source": "catalog",
            "celex": _NONCURATED_CELEX,
            "short_name": _NONCURATED_SHORT_NAME,
        },
    )

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

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["regulatory_instrument_id"] == "CRA-1.0"
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]


# --- ComplianceOfficer gate, catalog path only (issue #145) -----------------


def test_no_principal_at_all_is_denied_with_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #145, AC-BI-003/006: no verified principal -- 403, catalog pipeline never starts."""
    install_no_principal(monkeypatch)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "access_denied"
    assert body["error"]["message"] == "You do not have the required access role for this action."
    assert fake.recorder.order == []


def test_authenticated_user_without_compliance_officer_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified caller holding no elevated role at all -- still 403, pipeline never starts."""
    install_compliance_officer_grant(monkeypatch, granted=False)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert fake.recorder.order == []


def test_system_admin_without_explicit_grant_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: `SystemAdmin` alone does not implicitly satisfy `ComplianceOfficer`."""
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_ADMIN})
    )
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert fake.recorder.order == []


def test_system_owner_without_explicit_grant_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: `SystemOwner` alone does not implicitly satisfy `ComplianceOfficer` either."""
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_OWNER})
    )
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert fake.recorder.order == []
