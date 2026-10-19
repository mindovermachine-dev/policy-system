"""HTTP tests for ``POST /ingestions`` -- the catalog (CELEX) ingestion path.

Drives the route with ``TestClient`` and an
``app.dependency_overrides`` fake ``PipelineDependencies`` (``tests/api/_fakes.py``)
so request validation, catalog lookup, the pipeline hand-off, and error mapping
are exercised without a real graph, adapter, or LLM. The ``source: "internal"``
path is covered separately, in ``tests/api/test_ingestions_internal.py``. Every test runs as a
caller holding ``ComplianceOfficer``, the role both sources require.

Covers: the pipeline runs and reports its stages, an unknown CELEX is a 404 and a
malformed body a 422 (both before any stage), a stage failure is a 502 naming the
stage with a message free of paths / host:port, and the run id is in the body.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD
from fastapi.testclient import TestClient

from api._audit_fakes import InMemoryAuditStore
from api._fakes import (
    build_fake_pipeline_dependencies,
    compliance_officer_principal,
    default_cellar_rdf,
    default_cellar_xhtml,
    install_compliance_officer_grant,
    install_no_principal,
)
from ps_service.api.dependencies import provide_audit_store, provide_pipeline_dependencies
from ps_service.api.ingestion_orchestration import GraphOpeners
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.ingestion.adapters.errors import CellarFetchError, CellarNotFoundError
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.main import create_app

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import GraphHandle, PipelineDependencies

_VALID_CELEX = "32024R2847"
_VALID_SHORT_NAME = "cra"  # curated-content/catalog.json's own short_name for _VALID_CELEX
_UNKNOWN_CELEX = "32099R9999"
_UNKNOWN_SHORT_NAME = "unknown"
_HOST_PORT_RE = re.compile(r"\b[\w.\-]+:\d{2,5}\b")

# A CELEX absent from the curated catalog but well-formed and (per this
# fixture) resolvable via Cellar/ELI -- the Cellar fallback path.
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
    against a real emitter in ``test_ingestion_orchestration.py``.
    ``test_run_context.py`` exercises the real facade through the HTTP layer.
    """
    # detroit-exception: process-wide atexit logging facade, not a business fake
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
        company_merge_capability_merge_threshold=0.83,
        company_merge_capability_review_threshold=REVIEW_THRESHOLD,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _client_with_fake(
    fake_deps: PipelineDependencies, *, audit_store: InMemoryAuditStore | None = None
) -> TestClient:
    """A ``TestClient`` whose ``provide_pipeline_dependencies`` yields ``fake_deps``.

    The catalog path writes ``ingestion_run`` audit rows: they go to ``audit_store``
    (a fresh in-memory one by default) instead of Postgres.
    """
    app = create_app(_app_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake_deps
    store = audit_store if audit_store is not None else InMemoryAuditStore()
    app.dependency_overrides[provide_audit_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _grant_compliance_officer(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test below drives a caller holding `ComplianceOfficer` by default.

    `POST /ingestions` is gated by `require_access_role` as a route-level dependency, which has
    no local-test-bypass carve-out. The dedicated denial tests below re-monkeypatch this away
    for their own scenario (the same `monkeypatch` fixture instance, so the later call wins).
    """
    install_compliance_officer_grant(monkeypatch, granted=True)


def test_valid_celex_runs_pipeline_and_returns_run_id_and_stages() -> None:
    """A known CELEX runs all four stages and the body carries run id + stages."""
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


def test_legacy_celexless_node_already_merged_skips_pipeline_and_reports_already_ingested() -> None:
    """Legacy celex-less fallback: an already-merged node without ``celex`` is still reported.

    A ``RegulatoryInstrument`` that carries no ``celex`` property (written before
    ``celex`` was recorded) is invisible to the CELEX-existence check, so the
    ``{short_name}-{version}`` preflight still catches it: no stage runs and the body
    reports ``outcome="already_ingested"`` with an empty ``stages`` list. A node that
    does carry a CELEX is rejected earlier with 409 (see the ``celex_already_ingested``
    tests below); the fake's CELEX query is empty by default, which models the
    celex-less node.
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


def test_resubmitting_a_legacy_celexless_node_with_the_same_short_name_reports_already_ingested() -> (  # noqa: E501 - name states the legacy scenario in full
    None
):
    """Legacy celex-less fallback: re-submitting the same pair is still ``already_ingested``.

    Re-submitting the SAME ``(celex, short_name)`` pair against a legacy node with no
    ``celex`` property still reports ``already_ingested`` and runs no pipeline stage:
    the collision check does not trip against the CELEX's own row (its query excludes
    ``n.celex == $celex``), and the CELEX-existence check finds no celex-bearing node,
    so the ``_is_already_merged`` preflight decides. A celex-bearing node
    is rejected with 409 instead.
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


# --- CELEX-existence check ---------------------------------------------------


def test_reingesting_existing_celex_under_a_different_short_name_is_rejected_with_409() -> None:
    """A CELEX already in the live graph is rejected with 409
    ``celex_already_ingested`` before any stage runs, whatever ``short_name`` is asked
    for; the message names the short_name it is already ingested under.
    """
    fake = build_fake_pipeline_dependencies(celex_row="cra-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": "othername"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "celex_already_ingested"
    assert (
        body["error"]["message"] == f"CELEX {_VALID_CELEX} is already ingested as short_name 'cra'"
    )
    assert fake.recorder.order == []


def test_reingesting_existing_celex_under_the_same_short_name_is_rejected_with_409() -> None:
    """The same ``(celex, short_name)`` pair for a
    celex-bearing node is a 409, not a 200 ``already_ingested`` no-op.
    """
    fake = build_fake_pipeline_dependencies(celex_row="cra-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "celex_already_ingested"
    assert fake.recorder.order == []


def test_celex_check_graph_failure_fails_closed_as_502_stage_celex_check() -> None:
    """A graph failure in the CELEX-existence check is a 502 naming
    ``celex_check`` -- never read as "not ingested yet" -- and no stage runs.
    """
    fake = build_fake_pipeline_dependencies(celex_error=RuntimeError("boom"))
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 502
    assert response.json()["error"]["failing_stage"] == "celex_check"
    assert fake.recorder.order == []


def test_celex_rejection_body_contains_only_celex_and_short_name() -> None:
    """The 409 body leaks no Cypher, traceback, or exception text.

    The CELEX query is made to fail with a distinctive exception for the 502 case and
    to hit for the 409 case; neither body may carry query text or the exception repr.
    """
    hit = _client_with_fake(build_fake_pipeline_dependencies(celex_row="cra-1.0").dependencies)
    failing = _client_with_fake(
        build_fake_pipeline_dependencies(
            celex_error=RuntimeError("boom MATCH (n:RegulatoryInstrument)")
        ).dependencies
    )
    request = {"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME}

    rejected_text = hit.post("/ingestions", json=request).text
    failed_text = failing.post("/ingestions", json=request).text

    for text in (rejected_text, failed_text):
        assert "MATCH" not in text
        assert "Traceback" not in text
        assert "RuntimeError" not in text
        assert "boom" not in text
    assert _VALID_CELEX in rejected_text
    assert "cra" in rejected_text


def test_celex_check_queries_only_the_single_tenant_handle() -> None:
    """The CELEX-existence check reads the single-tenant graph only,
    with the CELEX as its sole parameter; the native and baseline graphs are never queried.

    CHARACTERIZATION for ``native.calls == []`` / ``baseline.calls == []`` (those hold
    before the change too); the single-tenant CELEX query call is the red half.
    """
    fake = build_fake_pipeline_dependencies(celex_row="cra-1.0")
    client = _client_with_fake(fake.dependencies)

    client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    celex_calls = [
        call for call in fake.single_tenant.calls if call.params == {"celex": _VALID_CELEX}
    ]
    assert len(celex_calls) == 1
    assert fake.native.calls == []
    assert fake.baseline.calls == []


# --- short_name normalization + case-insensitive collision ---------


def test_lowercase_short_name_collides_with_existing_uppercase_claim() -> None:
    """``cra`` collides with an existing ``CRA-1.0`` claimed by a
    different CELEX -- the comparison is case-insensitive -- and is rejected with 409
    ``short_name_collision`` before any stage runs. The message echoes the normalized
    (uppercase) short_name.
    """
    fake = build_fake_pipeline_dependencies(collision_row=("CRA-1.0", "32024R0001"))
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _NONCURATED_CELEX, "short_name": "cra"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "short_name_collision"
    assert body["error"]["message"] == "short_name 'CRA' is already claimed by CELEX 32024R0001"
    assert fake.recorder.order == []


@pytest.mark.parametrize("requested", ["cra", "Cra", "CRA"])
def test_ingest_stage_receives_uppercase_short_name_regardless_of_input_case(
    requested: str,
) -> None:
    """Whatever case the caller sends, the ingest stage -- which
    builds ``RegulatoryInstrument.id`` as ``{short_name}-{version}`` -- receives the
    uppercase short_name, so the id becomes ``CRA-1.0``.

    The fake ingest stage returns a constant rid, so only the kwarg it received is
    asserted; the id derivation itself is proved by the live-style
    end-to-end test in ``tests/mcp_interface``.
    """
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": requested},
    )

    assert response.status_code == 200
    assert fake.recorder.calls[0].kwargs["short_name"] == "CRA"


def test_graph_openers_receive_normalized_short_name_and_graph_names_stay_lowercase() -> None:
    """Only ``RegulatoryInstrument.id`` is uppercased; graph names stay lowercase.

    Graph names stay lowercase because every graph-name builder lowercases (``native_graph_name``,
    ``baseline_graph_name``) and restore and the change-check sweep open the same
    lowercase graphs (``cra_native``); uppercase names would fork the graphs in
    case-sensitive FalkorDB. So this test asserts the openers receive the normalized
    ``"CRA"`` and that the derived graph names are lowercase regardless of case.
    """
    fake = build_fake_pipeline_dependencies()
    opened_native: list[str] = []
    opened_baseline: list[str] = []

    def _open_native(config: ServiceConfig, short_name: str) -> GraphHandle:
        _ = config
        opened_native.append(short_name)
        return fake.native

    def _open_baseline(config: ServiceConfig, short_name: str) -> GraphHandle:
        _ = config
        opened_baseline.append(short_name)
        return fake.baseline

    dependencies = replace(
        fake.dependencies,
        graphs=GraphOpeners(
            native=_open_native,
            baseline=_open_baseline,
            single_tenant=fake.dependencies.graphs.single_tenant,
        ),
    )
    client = _client_with_fake(dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": "cra"},
    )

    assert response.status_code == 200
    assert opened_native == ["CRA"]
    assert opened_baseline == ["CRA"]
    assert native_graph_name("CRA") == native_graph_name("cra") == "cra_native"
    assert baseline_graph_name("CRA") == baseline_graph_name("cra") == "cra_baseline"


def test_ingestion_accepted_response_has_exactly_the_documented_fields() -> None:
    """Adding ``GET /ingestions/{run_id}``
    and the ``outcome`` field does not otherwise add or remove a
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
    """A CELEX absent from both the curated catalog and Cellar/ELI 404s
    before any stage runs.

    ``_UNKNOWN_CELEX`` is well-formed, so the Cellar-fallback path engages
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
    """A body that fails Pydantic validation 422s before any stage runs."""
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"]
    assert "run_id" in body
    assert fake.recorder.order == []


def test_missing_short_name_returns_422_structured_and_starts_no_pipeline() -> None:
    """A catalog-source body omitting the now-required ``short_name`` 422s
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


class _CountingStubFetch:
    """Counts calls to a Cellar fetch boundary stub, serving ``body`` for any CELEX."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.celexes: list[str] = []

    def __call__(self, celex: str) -> bytes:
        self.celexes.append(celex)
        return self.body


def test_curated_celex_with_non_catalog_short_name_is_accepted_and_resolved_via_cellar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CELEX that happens to be curated, submitted under a
    ``short_name`` the catalog does not carry, is accepted (200), resolved through
    Cellar exactly once, and the caller's own ``short_name`` reaches the ingest stage.
    """
    fetch_xhtml = _CountingStubFetch(_FIXTURE_REGULATION_A)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", fetch_xhtml)
    fake = build_fake_pipeline_dependencies(rid="mycra-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": "mycra"},
    )

    assert response.status_code == 200
    assert response.json()["regulatory_instrument_id"] == "mycra-1.0"
    assert fetch_xhtml.celexes == [_VALID_CELEX]
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]
    # The caller's short_name reaches the ingest stage normalized.
    assert fake.recorder.calls[0].kwargs["short_name"] == "MYCRA"


def test_celex_not_in_catalog_still_resolves_via_cellar(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CELEX absent from the curated catalog still resolves via Cellar.

    A CELEX absent from the curated catalog still resolves through Cellar under the
    caller's ``short_name``; the Cellar-fetch count is the discriminating assertion
    that the single resolution path serves it.
    """
    fetch_xhtml = _CountingStubFetch(_FIXTURE_REGULATION_A)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", fetch_xhtml)
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
    assert fetch_xhtml.celexes == [_NONCURATED_CELEX]
    assert fake.recorder.calls[0].kwargs["short_name"] == _NONCURATED_SHORT_NAME.upper()


def test_short_name_equal_to_another_curated_celexs_canonical_name_is_accepted_when_unclaimed_in_graph() -> (  # noqa: E501 - long descriptive test name
    None
):
    """``dora`` is the catalog's canonical name for a *different*
    CELEX, but nothing in the live graph claims it, so ingesting the CRA CELEX as
    ``dora`` is accepted. The catalog is not consulted for ``short_name`` claims;
    only the graph is.
    """
    fake = build_fake_pipeline_dependencies(rid="dora-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": "dora"},
    )

    assert response.status_code == 200
    assert fake.recorder.calls[0].kwargs["short_name"] == "DORA"


def test_curated_celex_cellar_outage_returns_502_and_never_uses_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Cellar outage for a curated CELEX fails the request as a
    502 naming the ``ingestion`` stage; there is no catalog fallback that would let the
    request through, and no pipeline stage runs.
    """

    def _failing_fetch(celex: str) -> bytes:
        raise CellarFetchError(f"Cellar/ELI fetch failed for CELEX {celex!r}")

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _failing_fetch)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 502
    assert response.json()["error"]["failing_stage"] == "ingestion"
    assert fake.recorder.order == []


def test_curated_celex_cellar_not_found_returns_404_with_no_catalog_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A curated CELEX that Cellar does not know is a 404, with no
    catalog fallback, and the message does not claim a curated catalog was consulted.
    """

    def _not_found_fetch(celex: str) -> bytes:
        raise CellarNotFoundError(f"CELEX {celex!r} was not found on Cellar/ELI")

    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", _not_found_fetch)
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 404
    message = response.json()["error"]["message"]
    assert _VALID_CELEX in message
    assert "curated" not in message
    assert fake.recorder.order == []


def test_short_name_collision_with_an_already_ingested_different_celex_is_rejected_before_any_pipeline_graph_is_opened() -> (  # noqa: E501 - long descriptive test name
    None
):
    """A non-curated CELEX whose ``short_name`` is already
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
        f"short_name '{_NONCURATED_SHORT_NAME.upper()}' is already claimed by CELEX 32024R0001"
    )
    assert "run_id" in body
    assert fake.recorder.order == []


# There is no catalog-vs-catalog `short_name` collision scenario: the catalog is not part
# of the ingest path, so only the graph-side collision check applies (covered by the
# graph-collision tests above and below).


def test_collision_check_graph_unreachable_fails_closed_into_502_before_any_pipeline_stage_runs() -> (  # noqa: E501 - long descriptive test name
    None
):
    """An I/O failure in the graph-side collision check
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
    """A stage failure 502s, names the stage, and leaks no path / host:port."""
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
    """A client-supplied ``run_id`` is used as the run's
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


# --- Cellar fallback, end-to-end over HTTP -----------------------------------


def test_non_curated_celex_resolves_via_cellar_and_runs_full_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed, non-curated CELEX resolves via
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
    assert fake.recorder.calls[0].kwargs["short_name"] == _NONCURATED_SHORT_NAME.upper()
    assert fake.recorder.calls[0].kwargs["version"] == "1.0"


def test_non_curated_celex_uses_caller_supplied_short_name_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resulting id is exactly ``{short_name}-{version}``,
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
    """``_derive_short_name`` is never called once the request
    supplies its own ``short_name`` for the catalog path's Cellar-fallback.

    The real, unpatched ``_derive_short_name`` runs (``resolve_via_cellar``'s
    own short-circuit -- ``short_name if short_name is not None else
    _derive_short_name(...)`` -- never reaches it when a ``short_name`` is
    supplied): its non-invocation is proven by the *output* state instead of
    an exploding double, exactly like
    ``test_non_curated_celex_uses_caller_supplied_short_name_verbatim``
    above -- the resulting id is the caller-supplied ``short_name`` verbatim,
    never a slug derived from the Cellar-fetched title.
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


def test_non_curated_celex_not_found_on_cellar_returns_404_before_any_stage_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-curated CELEX that Cellar/ELI also doesn't recognise 404s before
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
    """A Cellar/ELI outage (distinct from not-found) surfaces as a 502 naming
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
    """Each Cellar/ELI document (XHTML, RDF) is fetched exactly once for
    the whole HTTP round trip -- once during resolution, never again by the ingest
    stage. Direct end-to-end proof, over HTTP, of the "fetch-once, replay both
    cached payloads" regression guard -- without the
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


def test_curated_celex_is_resolved_via_cellar_like_any_other_celex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A curated CELEX takes the same Cellar resolution
    path as every other CELEX -- there is no catalog fast path -- so both Cellar
    documents are fetched exactly once for it.
    There is no path on which the Cellar fetch is skipped for a curated CELEX.
    """
    fetch_xhtml = _CountingStubFetch(default_cellar_xhtml(_VALID_CELEX))
    fetch_rdf = _CountingStubFetch(default_cellar_rdf(_VALID_CELEX))
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_xhtml", fetch_xhtml)
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.fetch_rdf", fetch_rdf)
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 200
    assert response.json()["regulatory_instrument_id"] == "CRA-1.0"
    assert fetch_xhtml.celexes == [_VALID_CELEX]
    assert fetch_rdf.celexes == [_VALID_CELEX]
    assert fake.recorder.order == ["ingestion", "extraction", "derivation", "merge"]


# --- ComplianceOfficer gate -------------------------


def test_no_principal_at_all_is_denied_with_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """No verified principal -- 403, catalog pipeline never starts."""
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
    """`SystemAdmin` alone does not implicitly satisfy `ComplianceOfficer`."""
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
    """`SystemOwner` alone does not implicitly satisfy `ComplianceOfficer` either."""
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


# --- ingestion_run audit rows (sync REST) ------------------------------------------------------


def test_post_ingestions_catalog_audits_the_principal_and_effective_run_id() -> None:
    """Submit + complete rows for the caller, keyed by the effective run id.

    The ComplianceOfficer gate (autouse) presents `compliance_officer_principal()`. A
    client-supplied ``run_id`` wins over the server-minted one, so it is the rows' ``resource_id``.
    (Under the local-test bypass this gated path is unreachable; the sentinel is covered by the MCP
    test and `provide_audit_context`.)
    """
    store = InMemoryAuditStore()
    fake = build_fake_pipeline_dependencies(rid="CRA-1.0")
    client = _client_with_fake(fake.dependencies, audit_store=store)
    principal = compliance_officer_principal()

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
    assert [(r.action, r.resource_id, r.outcome) for r in store.rows] == [
        ("ingestion_run.submit", "client-abc123", "applied"),
        ("ingestion_run.complete", "client-abc123", "applied"),
    ]
    assert {(r.actor_subject, r.actor_issuer) for r in store.rows} == {
        (principal.sub, principal.iss)
    }
    assert store.rows[0].details["trigger"] == "sync_ingest"
    assert store.rows[1].details["regulatory_instrument_id"] == "CRA-1.0"


def test_post_ingestions_internal_source_writes_no_audit_row() -> None:
    """The internal-document path is not an audited ingestion (no audit row is written)."""
    store = InMemoryAuditStore()
    client = _client_with_fake(build_fake_pipeline_dependencies().dependencies, audit_store=store)

    response = client.post(
        "/ingestions", json={"source": "internal", "content": {"not": "a valid seed"}}
    )

    assert response.status_code != 200
    assert store.rows == []


def test_post_ingestions_returns_503_when_audit_unavailable_and_runs_nothing() -> None:
    """An unwritable `ingestion_run.submit` row is a 503 and no stage ran."""
    store = InMemoryAuditStore(
        fail_on_outcome={"applied": AuditPostgresUnavailableError("db down")}
    )
    fake = build_fake_pipeline_dependencies()
    client = _client_with_fake(fake.dependencies, audit_store=store)

    response = client.post(
        "/ingestions",
        json={"source": "catalog", "celex": _VALID_CELEX, "short_name": _VALID_SHORT_NAME},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_trail_unavailable"
    assert "db down" not in response.text
    assert fake.recorder.calls == []
    assert store.rows == []
