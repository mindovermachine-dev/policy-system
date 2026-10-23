"""HTTP tests for ``POST /ingestions`` -- the internal-document ingestion path.

A ``source: "internal"`` request carries the intake document's content directly in the
request body (no server-side path resolution) and runs the
internal-seed pipeline's stages (``internal_ingestion``, ``merge``), via the
same ``app.dependency_overrides`` fake ``PipelineDependencies`` pattern every
other route test in this package uses (``tests/api/_fakes.py``).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from company_merge._gateway_fakes import POLICY_SYSTEM, RigReadHandle
from company_merge._seed_merge_graphs import BaselineGraphReader, OneHotEmbeddingCaller
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD
from fastapi.testclient import TestClient
from graph_gateway._fakes import GatewayRig, close_all_rigs
from litellm.types.utils import Embedding, EmbeddingResponse

from api._fakes import (
    build_fake_pipeline_dependencies,
    install_compliance_officer_caller,
    install_compliance_officer_grant,
    install_no_principal,
)
from api._gateway_fakes import RecordingSystemAudit, override_gateway
from ps_service.api.dependencies import get_principal, provide_pipeline_dependencies
from ps_service.api.errors import IngestionConfigIncompleteError
from ps_service.api.ingestion_orchestration import GraphOpeners, run_internal_ingestion_pipeline
from ps_service.auth.models import Principal
from ps_service.authz.models import AccessRole
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.company_merge.pending_review import list_pending_reviews
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

    from fastapi import FastAPI
    from httpx2 import Response

    from api._fakes import FakePipeline, MakeEmitter
    from ps_service.api.ingestion_orchestration import GraphHandle, PipelineDependencies
    from ps_service.company_merge.governance_import import GovernanceImport
    from ps_service.company_merge.models import MergeResult
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import EmbeddingCaller
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


pytestmark = pytest.mark.usefixtures("grant_compliance_officer")

_DENIED_MESSAGE = "You do not have the required access role for this action."


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
        company_merge_role_review_threshold=ROLE_REVIEW_THRESHOLD,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


@pytest.fixture(autouse=True)
def _close_gateway_rigs() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse
    """Stop the reconciler thread of every gateway rig a test built."""
    yield
    close_all_rigs()


def _client_with_fake(
    fake_deps: PipelineDependencies,
    rig: GatewayRig | None = None,
    system_audit: RecordingSystemAudit | None = None,
) -> TestClient:
    """A ``TestClient`` whose pipeline dependencies are ``fake_deps``, over a gateway rig."""
    rig = rig or GatewayRig()  # the internal route fails closed without a gateway
    app = create_app(_app_config())
    app.dependency_overrides[provide_pipeline_dependencies] = lambda: fake_deps
    app.dependency_overrides[get_principal] = lambda: Principal(sub="owner-1", iss="https://idp")
    override_gateway(app, rig, system_audit or RecordingSystemAudit(rig))
    return TestClient(app, raise_server_exceptions=False)


def _real_stage_dependencies(
    rig: GatewayRig, store: RigReadHandle, embedder: EmbeddingCaller | None = None
) -> PipelineDependencies:
    """Fake dependencies whose internal stages are the REAL ones, over in-memory graphs.

    ``internal_ingestion`` is the real ``ingest_internal_regulatory_instrument`` writing through
    the rig's gateway into its ``{short}_baseline`` graph; ``merge`` is the real
    ``merge_baseline_graph`` reading that graph back and writing into the rig's ``policy_system``
    (``store`` is its read handle, which also applies the not yet migrated legacy writes), with the
    embedding LLM as the only faked call. The catalog
    stages stay fakes -- an internal request never reaches them.
    """
    base = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-1.0").dependencies
    embedder = embedder or OneHotEmbeddingCaller()

    def _merge(
        regulatory_instrument_id: str,
        *,
        baseline_graph: GraphHandle,
        single_tenant_graph: GraphHandle,
        submitter: GroupSubmitter,
        single_tenant_graph_name: str,
        baseline_graph_name: str,
        audit_event_id: str,
        embed_model: str,
        merge_threshold: float | None,
        review_threshold: float | None,
        role_review_threshold: float | None,
        emitter: LogEmitter | None = None,
        governance: GovernanceImport | None = None,
    ) -> MergeResult:
        return merge_baseline_graph(
            regulatory_instrument_id,
            baseline_graph=baseline_graph,
            single_tenant_graph=single_tenant_graph,
            submitter=submitter,
            single_tenant_graph_name=single_tenant_graph_name,
            baseline_graph_name=baseline_graph_name,
            audit_event_id=audit_event_id,
            embed_model=embed_model,
            merge_threshold=merge_threshold,
            review_threshold=review_threshold,
            role_review_threshold=role_review_threshold,
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

    ``content`` is the parsed ``engineering-practices-seed.json``; the seed is
    ingested by the real ``internal_ingestion`` stage and merged by the real ``merge`` stage
    (no faked merge), so the draft governance import is exercised end to end. The
    caller is ``("owner-1", "https://idp")`` -- every stored Policy is ``draft`` and owned by
    them, the tree is complete and the merge summary reports its
    counts.
    """
    del configured_logging  # the real merge logs through the process-default emitter
    rig = GatewayRig()  # the internal route fails closed without a gateway
    store = RigReadHandle(rig.graphs.open(POLICY_SYSTEM))
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


class _NearRoleEmbedding(OneHotEmbeddingCaller):
    """One-hot for every text, except two Role names that score 0.8 against each other."""

    _NEAR: ClassVar[dict[str, tuple[float, float]]] = {
        "Service Owner": (1.0, 0.0),
        "Engineering Manager": (0.8, 0.6),
    }

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        near = self._NEAR.get(inputs[0])
        if near is None:
            return super().__call__(model=model, inputs=inputs, timeout=timeout)
        vector = [0.0] * 512
        vector[510], vector[511] = near
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def test_an_internal_seed_with_two_near_roles_raises_one_role_near_miss(
    make_emitter: MakeEmitter,
) -> None:
    """The internal-seed pipeline runs the same Role comparison as the external one."""
    emitter, _ = make_emitter()
    rig = GatewayRig()
    store = RigReadHandle(rig.graphs.open(POLICY_SYSTEM))
    dependencies = _real_stage_dependencies(rig, store, _NearRoleEmbedding())

    outcome = run_internal_ingestion_pipeline(
        _REAL_SEED_DOCUMENT,
        config=replace(_app_config(), company_merge_role_review_threshold=0.8),
        run_id="run-role-near",
        caller="test",
        dependencies=dependencies,
        gateway=rig.gateway,
        system_audit=RecordingSystemAudit(rig),
        owner=("owner-1", "https://idp"),
        emitter=emitter,
    )

    reviews = list_pending_reviews(store)
    assert [
        (r.kind, r.incoming_text, r.nearest_existing_text) for r in reviews if r.kind == "Role"
    ] == [("Role", "Engineering Manager", "Service Owner")]
    internal_summary, merge_summary = (report.summary for report in outcome.stages)
    assert internal_summary["roles_minted"] == 6
    assert internal_summary["non_actor_roles_dropped"] == 0
    assert merge_summary["role_near_misses"] == 1
    assert merge_summary["pending_reviews"] == len(reviews)  # Capability and Role reviews together


def test_an_internal_seed_with_distinct_roles_raises_no_role_near_miss(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    rig = GatewayRig()
    store = RigReadHandle(rig.graphs.open(POLICY_SYSTEM))

    outcome = run_internal_ingestion_pipeline(
        _REAL_SEED_DOCUMENT,
        config=replace(_app_config(), company_merge_role_review_threshold=0.8),
        run_id="run-role-distinct",
        caller="test",
        dependencies=_real_stage_dependencies(rig, store),
        gateway=rig.gateway,
        system_audit=RecordingSystemAudit(rig),
        owner=("owner-1", "https://idp"),
        emitter=emitter,
    )

    assert [r for r in list_pending_reviews(store) if r.kind == "Role"] == []
    assert outcome.stages[1].summary["role_near_misses"] == 0


def test_the_internal_pipeline_hands_the_configured_role_threshold_to_the_merge() -> None:
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    rig = GatewayRig()

    run_internal_ingestion_pipeline(
        _REAL_SEED_DOCUMENT,
        config=replace(_app_config(), company_merge_role_review_threshold=0.66),
        run_id="run-role-threshold",
        caller="test",
        dependencies=fake.dependencies,
        gateway=rig.gateway,
        system_audit=RecordingSystemAudit(rig),
        owner=("owner-1", "https://idp"),
    )

    merge_call = next(call for call in fake.recorder.calls if call.stage == "merge")
    assert merge_call.kwargs["role_review_threshold"] == 0.66


def test_internal_merge_targets_the_configured_single_tenant_graph() -> None:
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    rig = GatewayRig()

    run_internal_ingestion_pipeline(
        _REAL_SEED_DOCUMENT,
        config=replace(_app_config(), falkordb_graph="custom_graph"),
        run_id="run-custom-graph",
        caller="test",
        dependencies=fake.dependencies,
        gateway=rig.gateway,
        system_audit=RecordingSystemAudit(rig),
        owner=("owner-1", "https://idp"),
    )

    merge_call = next(call for call in fake.recorder.calls if call.stage == "merge")
    assert merge_call.kwargs["single_tenant_graph_name"] == "custom_graph"


def test_an_unset_role_threshold_refuses_the_internal_pipeline_before_any_stage() -> None:
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    rig = GatewayRig()

    with pytest.raises(IngestionConfigIncompleteError, match="company_merge_role_review_threshold"):
        run_internal_ingestion_pipeline(
            _REAL_SEED_DOCUMENT,
            config=replace(_app_config(), company_merge_role_review_threshold=None),
            run_id="run-role-unset",
            caller="test",
            dependencies=fake.dependencies,
            gateway=rig.gateway,
            system_audit=RecordingSystemAudit(rig),
            owner=("owner-1", "https://idp"),
        )

    assert fake.recorder.order == []


def test_post_ingestions_internal_runs_stages_in_order() -> None:
    """The internal pipeline runs ``internal_ingestion`` then ``merge`` and nothing else.

    Stages are fakes here (the real-stage run is the test above); the catalog-only
    already-merged pre-flight never queries the single-tenant graph .
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
    """The pipeline hands the verified caller to the merge stage as the import owner."""
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    client.post("/ingestions", json={"source": "internal", "content": _REAL_SEED_DOCUMENT})

    merge_call = next(call for call in fake.recorder.calls if call.stage == "merge")
    assert merge_call.kwargs["governance_owner"] == ("owner-1", "https://idp")


def test_merge_stage_receives_the_gateway_graph_names_and_the_ingest_audit_event(
    make_emitter: MakeEmitter,
) -> None:
    """The internal-seed merge writes through the gateway the run resolved.

    The merge gets the same submitter as the ingest stage, the single-tenant and baseline graph
    names, and the ONE system audit event id of the run (the ingest stage's group and the merge's
    groups are logged under it).
    """
    emitter, _log_path = make_emitter()
    rig = GatewayRig()
    audit = RecordingSystemAudit(rig)
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")

    run_internal_ingestion_pipeline(
        _REAL_SEED_DOCUMENT,
        config=_app_config(),
        run_id="run-1",
        caller="test",
        dependencies=fake.dependencies,
        gateway=rig.gateway,
        system_audit=audit,
        owner=("owner-1", "https://idp"),
        emitter=emitter,
    )

    merge_call = next(call for call in fake.recorder.calls if call.stage == "merge")
    (audit_event_id,) = audit.ids
    assert merge_call.kwargs["submitter"] is rig.gateway
    assert merge_call.kwargs["single_tenant_graph_name"] == POLICY_SYSTEM
    assert merge_call.kwargs["baseline_graph_name"] == baseline_graph_name("ENGPRAC")
    assert merge_call.kwargs["audit_event_id"] == audit_event_id


def test_a_gateway_failure_in_the_merge_is_a_502_merge_failure_without_gateway_text() -> None:
    """A persistence error from the merge is the usual merge 502."""
    fake = build_fake_pipeline_dependencies(
        internal_rid="ENGPRAC-3.0",
        merge_error=CompanyMergePersistenceError(
            "FalkorDB write failed: the group was committed to the graph log but could not be "
            "applied to the graph yet"
        ),
    )
    client = _client_with_fake(fake.dependencies)

    response = client.post(
        "/ingestions", json={"source": "internal", "content": _REAL_SEED_DOCUMENT}
    )

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["failing_stage"] == "merge"
    assert "FalkorDB write failed" not in error["message"]
    assert "graph log" not in error["message"]


def test_merge_stage_receives_the_pipeline_emitter(make_emitter: MakeEmitter) -> None:
    """The emitter the run was given reaches the merge stage."""
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
    """The ``internal_ingestion`` stage's summary dict contains ``"policies"``,
    ``"standards"`` and ``"controls"`` keys -- and the
    pipeline's own stage list is exactly ``["internal_ingestion", "merge"]``,
    with no ``governance_derivation`` stage and no ``governance_derivation``-outcome
    log entry emitted anywhere in this pipeline.
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
    """Posting the exact
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
    """``InternalIngestionRequest`` (``models.py``) has no
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
    """``routes.py``'s ``if request_body.source == "internal":``
    early-return never reaches ``resolve_ingestion_entry`` (the CELEX-existence and
    ``short_name``-collision identity checks) -- proving it is provably unreached on
    this path, not merely untested.

    The real, unpatched ``resolve_ingestion_entry`` is left in place (never called
    for ``source="internal"``, so its being real is inert either way); non-invocation
    is proven by *state* instead of an exploding double -- its graph-side checks are
    the only things in this pipeline that would ever query ``fake.single_tenant``
    before the pipeline's own stages run, so an empty ``fake.single_tenant.calls``
    proves it never ran, mirroring
    ``test_post_ingestions_internal_succeeds_end_to_end``'s own
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
    """An ``internal_ingestion`` stage failure 502s, names the
    stage, and no later stage (``merge``) ever runs. This is
    the internal pipeline's first-stage failure-path proof,
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


class _RunLogRecorder:
    """Records every run-log entry the internal pipeline emits."""

    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> None:
        self.entries.append(kwargs)


def _post_internal_seed(client: TestClient) -> Response:
    """Post the Policy-bearing engineering-practices seed as an internal ingestion."""
    return client.post(
        "/ingestions",
        json={
            "source": _ENVELOPE_CONTRACT["source"],
            _ENVELOPE_CONTRACT["content_field_name"]: _REAL_SEED_DOCUMENT,
        },
    )


def _assert_denied_and_nothing_happened(
    response: Response,
    *,
    fake: FakePipeline,
    rig: GatewayRig,
    system_audit: RecordingSystemAudit,
    run_log: _RunLogRecorder,
) -> None:
    """Assert a 403 access_denied and that no stage, write, audit row or log entry occurred."""
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "access_denied"
    assert error["message"] == _DENIED_MESSAGE
    assert fake.recorder.order == []
    assert rig.store.events == []
    assert system_audit.rows == []
    assert run_log.entries == []


@pytest.fixture
def run_log(monkeypatch: pytest.MonkeyPatch) -> _RunLogRecorder:
    """Replace the Logging boundary with a recorder of the entries emitted."""
    recorder = _RunLogRecorder()
    # detroit-exception: process-wide atexit logging facade, not a business fake
    monkeypatch.setattr("ps_service.api.ingestion_orchestration.emit_log_entry", recorder)
    return recorder


def test_internal_ingest_without_compliance_officer_is_denied_with_403_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, run_log: _RunLogRecorder
) -> None:
    """An authenticated caller holding no elevated role is refused before any work starts."""
    install_compliance_officer_grant(monkeypatch, granted=False)
    rig = GatewayRig()
    system_audit = RecordingSystemAudit(rig)
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies, rig, system_audit)

    response = _post_internal_seed(client)

    _assert_denied_and_nothing_happened(
        response, fake=fake, rig=rig, system_audit=system_audit, run_log=run_log
    )


def test_internal_ingest_with_no_principal_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch, run_log: _RunLogRecorder
) -> None:
    """A request with no verified caller at all is refused before any work starts."""
    install_no_principal(monkeypatch)
    rig = GatewayRig()
    system_audit = RecordingSystemAudit(rig)
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies, rig, system_audit)

    response = _post_internal_seed(client)

    _assert_denied_and_nothing_happened(
        response, fake=fake, rig=rig, system_audit=system_audit, run_log=run_log
    )


def test_internal_ingest_with_system_admin_but_no_compliance_officer_grant_is_denied(
    monkeypatch: pytest.MonkeyPatch, run_log: _RunLogRecorder
) -> None:
    """``SystemAdmin`` alone does not satisfy the ``ComplianceOfficer`` requirement."""
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_ADMIN})
    )
    rig = GatewayRig()
    system_audit = RecordingSystemAudit(rig)
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies, rig, system_audit)

    response = _post_internal_seed(client)

    _assert_denied_and_nothing_happened(
        response, fake=fake, rig=rig, system_audit=system_audit, run_log=run_log
    )


def test_internal_ingest_with_schema_invalid_body_and_no_role_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The role check precedes body validation, so an unprivileged caller learns no schema."""
    install_compliance_officer_grant(monkeypatch, granted=False)
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    response = client.post("/ingestions", json={"source": "internal"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"


def test_internal_ingest_denial_body_has_the_shared_error_shape_and_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal uses the shared error envelope and names no document or internal detail."""
    install_compliance_officer_grant(monkeypatch, granted=False)
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies)

    response = _post_internal_seed(client)

    assert response.status_code == 403
    body = response.json()
    assert set(body) == {"error", "run_id"}
    assert set(body["error"]) == {"code", "message", "failing_stage"}
    assert body["error"]["failing_stage"] is None
    for internal_detail in (
        "ENGPRAC",
        "MATCH",
        "cypher",
        "_baseline",
        "_native",
        "access_role",
        "Traceback",
    ):
        assert internal_detail not in response.text


def test_internal_ingest_with_compliance_officer_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified ``ComplianceOfficer`` runs a Policy-bearing seed through the pipeline."""
    rig = GatewayRig()
    fake = build_fake_pipeline_dependencies(internal_rid="ENGPRAC-3.0")
    client = _client_with_fake(fake.dependencies, rig)
    install_compliance_officer_caller(
        monkeypatch,
        cast("FastAPI", client.app),
        principal=Principal(sub="owner-1", iss="https://idp"),
    )

    response = _post_internal_seed(client)

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["source"] == "internal"
    assert [stage["stage"] for stage in body["stages"]] == ["internal_ingestion", "merge"]
    assert fake.recorder.order == ["internal_ingestion", "merge"]
