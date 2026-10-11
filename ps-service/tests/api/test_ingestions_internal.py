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
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from company_merge._seed_merge_graphs import (
    BaselineGraphReader,
    OneHotEmbeddingCaller,
    SingleTenantStore,
)
from fastapi.testclient import TestClient
from graph_gateway._fakes import GatewayRig, close_all_rigs

from api._fakes import build_fake_pipeline_dependencies
from api._gateway_fakes import RecordingSystemAudit, override_gateway
from ps_service.api.dependencies import get_principal, provide_pipeline_dependencies
from ps_service.api.ingestion_orchestration import GraphOpeners, run_internal_ingestion_pipeline
from ps_service.auth.models import Principal
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.config import ServiceConfig
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.persist import (
    ingest_internal_regulatory_instrument,
)
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from api._fakes import MakeEmitter
    from ps_service.api.ingestion_orchestration import GraphHandle, PipelineDependencies
    from ps_service.company_merge.governance_import import GovernanceImport
    from ps_service.company_merge.models import MergeResult
    from ps_service.logging import LogEmitter

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
    (
        _REPO_ROOT
        / "ps-service"
        / "tests"
        / "fixtures"
        / "wire-contracts"
        / "ingest-internal-envelope.json"
    ).read_text(encoding="utf-8")
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
    # detroit-exception: process-wide atexit logging facade (AUDIT §2 case 12), not a business fake
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


@pytest.fixture(autouse=True)
def _close_gateway_rigs() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Stop the reconciler thread of every gateway rig a test built."""
    yield
    close_all_rigs()


def _client_with_fake(fake_deps: PipelineDependencies, rig: GatewayRig | None = None) -> TestClient:
    """A ``TestClient`` whose pipeline dependencies are ``fake_deps``, over a gateway rig."""
    rig = rig or GatewayRig()  # the internal route fails closed without a gateway (issue #208)
    app = create_app(_app_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake_deps
    app.dependency_overrides[get_principal] = lambda: Principal(sub="owner-1", iss="https://idp")
    override_gateway(app, rig, RecordingSystemAudit(rig))
    return TestClient(app, raise_server_exceptions=False)


def _real_stage_dependencies(rig: GatewayRig, store: SingleTenantStore) -> PipelineDependencies:
    """Fake dependencies whose internal stages are the REAL ones, over in-memory graphs.

    ``internal_ingestion`` is the real ``ingest_internal_regulatory_instrument`` writing through
    the rig's gateway into its ``{short}_baseline`` graph; ``merge`` is the real
    ``merge_baseline_graph`` reading that graph back and writing into ``store`` (the
    ``policy_system`` stand-in), with the embedding LLM as the only faked call. The catalog
    stages stay fakes -- an internal request never reaches them.
    """
    base = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-1.0").dependencies
    embedder = OneHotEmbeddingCaller()

    def _merge(
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        single_tenant_graph: GraphHandle,
        embed_model: str,
        similarity_threshold: float | None,
        emitter: LogEmitter | None = None,
        governance: GovernanceImport | None = None,
    ) -> MergeResult:
        return merge_baseline_graph(
            regulatory_instrument_id,
            baseline_graph=baseline_graph,
            single_tenant_graph=single_tenant_graph,
            embed_model=embed_model,
            similarity_threshold=similarity_threshold,
            call_embedding=embedder,
            emitter=emitter,
            governance=governance,
        )

    return replace(
        base,
        graphs=GraphOpeners(
            native=lambda _config, short_name: rig.graphs.open(native_graph_name(short_name)),
            baseline=lambda _config, short_name: BaselineGraphReader(
                rig.graphs.open(baseline_graph_name(short_name))
            ),
            single_tenant=lambda _config: store,
        ),
        stages=replace(
            base.stages, ingest_internal=ingest_internal_regulatory_instrument, merge=_merge
        ),
    )


def test_post_ingestions_internal_runs_real_pipeline(configured_logging: Path) -> None:
    """A well-formed internal request runs the real ``internal_ingestion`` and ``merge`` stages.

    Issue #227: ``content`` is the parsed ``engineering-practices-seed.json``; the seed is
    ingested by the real ``internal_ingestion`` stage and merged by the real ``merge`` stage
    (no faked merge any more), so the draft governance import is exercised end to end. The
    caller is ``("owner-1", "https://idp")`` -- every stored Policy is ``draft`` and owned by
    them (AC-BI-002), the tree is complete (AC-BI-004) and the merge summary reports its
    counts (AC-BI-011).
    """
    del configured_logging  # the real merge logs through the process-default emitter
    rig = GatewayRig()  # the internal route fails closed without a gateway (issue #208)
    store = SingleTenantStore()
    client = _client_with_fake(_real_stage_dependencies(rig, store), rig=rig)

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
    assert body["regulatory_instrument_id"] == "ENGPRAC-1.0"
    assert body["source"] == "internal"
    assert body["outcome"] == "fresh"
    assert [stage["stage"] for stage in body["stages"]] == ["internal_ingestion", "merge"]
    assert all(stage["status"] == "succeeded" for stage in body["stages"])
    policies = store.nodes_of("Policy")
    assert len(policies) == 11
    for policy in policies.values():
        assert policy["status"] == "draft"
        assert (policy["owner_subject"], policy["owner_issuer"]) == ("owner-1", "https://idp")
    assert (len(store.nodes_of("Standard")), len(store.nodes_of("Control"))) == (21, 39)
    merge_summary = body["stages"][1]["summary"]
    assert merge_summary["governance_policies"] == 11
    assert merge_summary["governance_standards"] == 21
    assert merge_summary["governance_controls"] == 39
    assert merge_summary["governance_status_overridden"] == 0


def test_post_ingestions_internal_runs_stages_in_order() -> None:
    """The internal pipeline runs ``internal_ingestion`` then ``merge`` and nothing else.

    Stages are fakes here (the real-stage run is the test above); the catalog-only
    already-merged pre-flight never queries the single-tenant graph (AC-BI-005, issue #135).
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
    assert fake.recorder.order == ["internal_ingestion", "merge"]
    assert fake.single_tenant.calls == []


def test_merge_stage_receives_the_caller_as_governance_owner() -> None:
    """Issue #227: the pipeline hands the verified caller to the merge stage as the import owner."""
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    client.post("/ingestions", json={"source": "internal", "content": _REAL_SEED_DOCUMENT})

    merge_call = next(call for call in fake.recorder.calls if call.stage == "merge")
    assert merge_call.kwargs["governance_owner"] == ("owner-1", "https://idp")


def test_merge_stage_receives_the_pipeline_emitter(make_emitter: MakeEmitter) -> None:
    """Issue #227: the emitter the run was given reaches the merge stage."""
    emitter, _log_path = make_emitter()
    rig = GatewayRig()
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")

    run_internal_ingestion_pipeline(
        _REAL_SEED_DOCUMENT,
        config=_app_config(),
        run_id="run-1",
        caller="test",
        dependencies=fake.dependencies,
        gateway=rig.gateway,
        system_audit=RecordingSystemAudit(rig),
        owner=("owner-1", "https://idp"),
        emitter=emitter,
    )

    merge_call = next(call for call in fake.recorder.calls if call.stage == "merge")
    assert merge_call.kwargs["emitter"] is emitter


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


def test_internal_source_request_still_forbids_an_unexpected_short_name_field() -> None:
    """Issue #146 AC-BI-009: ``InternalIngestionRequest`` (``models.py``) has no
    ``short_name`` field, and its existing ``extra="forbid"`` config rejects
    one with 422 -- proving the model's shape is genuinely untouched by the
    catalog-only ``short_name`` requirement, not just "no diff seen".
    """
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions",
        json={"source": "internal", "content": _REAL_SEED_DOCUMENT, "short_name": "x"},
    )

    assert response.status_code == 422


def test_internal_ingestion_never_calls_resolve_ingestion_entry() -> None:
    """Issue #146 AC-BI-009 / issue #193: ``routes.py``'s ``if request_body.source == "internal":``
    early-return never reaches ``resolve_ingestion_entry`` (the CELEX-existence and
    ``short_name``-collision identity checks) -- proving it is provably unreached on
    this path, not merely untested.

    The real, unpatched ``resolve_ingestion_entry`` is left in place (never called
    for ``source="internal"``, so its being real is inert either way); non-invocation
    is proven by *state* instead of an exploding double -- its graph-side checks are
    the only things in this pipeline that would ever query ``fake.single_tenant``
    before the pipeline's own stages run, so an empty ``fake.single_tenant.calls``
    proves it never ran, mirroring
    ``test_post_ingestions_internal_succeeds_end_to_end``'s own AC-BI-005
    assertion just above.
    """
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions", json={"source": "internal", "content": _REAL_SEED_DOCUMENT}
    )

    assert response.status_code == 200
    assert fake.single_tenant.calls == []


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
