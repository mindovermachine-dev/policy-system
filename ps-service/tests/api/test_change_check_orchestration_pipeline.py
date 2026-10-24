"""The `check-regulations` sweep runs the full pipeline, succession last.

Entry is the real `run_change_check_sweep`; the code under test includes the REAL
`trigger_reingestion` / `will_reingest` / `classify_reingestion` / `succession`. The boundaries
are doubles: the `{short}_native` and `policy_system` graphs are the gateway rig's in-memory
graphs (the succession is written to them only as gateway groups), the four pipeline stages are
the hand-written `FakePipeline` stage seam (recording into a `LedgerStageRecorder` on the rig's
event list, so stage calls and logged groups land on ONE ordered timeline).
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from change_monitor._fakes import FakeAdapter
from change_monitor.test_trigger import (
    _structure,  # pyright: ignore[reportPrivateUsage]  -- reuse the trigger tests' canned metadata
)
from company_merge._thresholds import REVIEW_THRESHOLD, ROLE_REVIEW_THRESHOLD

from api._fakes import (
    LedgerStageRecorder,
    build_fake_change_check_dependencies,
    build_fake_pipeline_dependencies,
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import (
    ChangeCheckDependencies,
    ChangeCheckResult,
    run_change_check_sweep,
)
from ps_service.audit import AuditContext
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.change_monitor.models import AmendmentFinding, PollReport, TrackedInstrumentNode
from ps_service.change_monitor.trigger import trigger_reingestion, will_reingest
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
    from change_monitor._gateway_fakes import SuccessionRig

    from api._audit_fakes import InMemoryAuditStore
    from api._fakes import FakePipeline, MakeEmitter

_CELEX = "32024R2847"
_NEW_CELEX = "32024R2847C01"
_PRIOR = "CRA-1.0"
_NEW = f"CRA-{_NEW_CELEX}"
_ALL = ("ingestion", "extraction", "derivation", "merge")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    llm_interface_model="azure/gpt-4o",
    llm_interface_embed_model="azure/text-embedding-3-large",
    company_merge_capability_merge_threshold=0.83,
    company_merge_capability_review_threshold=REVIEW_THRESHOLD,
    company_merge_role_review_threshold=ROLE_REVIEW_THRESHOLD,
)


def _node(instrument_id: str = _PRIOR) -> TrackedInstrumentNode:
    return TrackedInstrumentNode(
        regulatory_instrument_id=instrument_id,
        celex=_CELEX,
        instrument_type="regulation",
        effective_date="2024-01-01",
    )


def _finding(instrument_id: str = _PRIOR) -> AmendmentFinding:
    return AmendmentFinding(
        regulatory_instrument_id=instrument_id,
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex=_NEW_CELEX,
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )


def _world(rig: SuccessionRig) -> SuccessionRig:
    """The prior is active in the native graph; `policy_system` holds both versions as active."""
    rig.seed_instrument(rig.native, _PRIOR)
    rig.seed_instrument(rig.single_tenant, _PRIOR)
    rig.seed_instrument(rig.single_tenant, _NEW)
    return rig


def _pipeline(rig: SuccessionRig, **kwargs: object) -> FakePipeline:
    recorder = LedgerStageRecorder(
        rig.rig.events, on_ingest=lambda _call: rig.seed_instrument(rig.native, _NEW)
    )
    return build_fake_pipeline_dependencies(recorder=recorder, rid=_NEW, **kwargs)  # pyright: ignore[reportArgumentType]


def _sweep(
    rig: SuccessionRig,
    pipeline: FakePipeline,
    store: InMemoryAuditStore,
    emitter: object,
    *,
    config: ServiceConfig = _CONFIG,
) -> ChangeCheckResult:
    fake = build_fake_change_check_dependencies(
        tracked=(_node(),),
        poll_report=PollReport(
            findings=(_finding(),), polled_count=1, failed_ids=(), unconfigured_ids=()
        ),
        catalog_entries={
            _CELEX: CatalogEntry(celex=_CELEX, title="CRA", short_name="CRA", version="1.0")
        },
        reingestion_result=None,
        pipeline=pipeline,
    )
    deps: ChangeCheckDependencies = dataclasses.replace(
        fake.dependencies,
        open_single_tenant=lambda _config: rig.read_single_tenant(),  # pyright: ignore[reportUnknownLambdaType]
        open_native=lambda _config, _short: rig.read_native(),  # pyright: ignore[reportUnknownLambdaType]
        default_adapter=lambda: FakeAdapter({_CELEX: _structure()}),
        trigger_reingestion=trigger_reingestion,  # pyright: ignore[reportArgumentType]
        will_reingest=will_reingest,  # pyright: ignore[reportArgumentType]
    )
    return run_change_check_sweep(
        config=config,
        run_id="sweep",
        dependencies=deps,
        audit=AuditContext(("caller", "https://issuer.example.com/"), store),
        emitter=emitter,  # pyright: ignore[reportArgumentType]
    )


def test_fresh_amendment_runs_all_four_stages_in_order_then_writes_succession_last(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    pipeline = _pipeline(rig)

    result = _sweep(rig, pipeline, rig.audit_store(), emitter)

    assert pipeline.recorder.order == list(_ALL)
    timeline = rig.timeline()
    stage_and_link = [e for e in timeline if e.startswith("stage:") or "SUPERSEDED_BY" in e]
    assert stage_and_link == [f"stage:{stage}" for stage in _ALL] + [
        "group:cra_native:SUPERSEDED_BY",
        "group:policy_system:SUPERSEDED_BY",
    ]
    # nothing may be superseded before the merge stage has been called
    assert timeline.index("group:cra_native:SUPERSEDED_BY") > timeline.index("stage:merge")
    [outcome] = result.instruments
    assert outcome.outcome == "amendment_reingested"
    assert outcome.detail == f"{_NEW} (superseded, fresh)"
    assert rig.edge_properties(rig.native, _PRIOR, _NEW) == [
        {"identity": f"SUPERSEDED_BY:{_PRIOR}->{_NEW}", "absorbed": True}
    ]
    assert rig.status_of(rig.native, _PRIOR) == "superseded"
    assert rig.marker_stage(rig.native, _NEW) is None


def test_the_audit_pair_brackets_the_run_and_shares_its_run_id_with_the_stages(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    pipeline = _pipeline(rig)
    store = rig.audit_store()

    result = _sweep(rig, pipeline, store, emitter)

    [outcome] = result.instruments
    assert [row.action for row in store.rows] == ["ingestion_run.submit", "ingestion_run.complete"]
    assert outcome.reingest_run_id == store.rows[0].resource_id == store.rows[1].resource_id
    ingest_call = pipeline.recorder.calls[0]
    assert ingest_call.kwargs["run_id"] == outcome.reingest_run_id
    assert ingest_call.kwargs["version"] == _NEW_CELEX


def test_every_succession_group_carries_the_opening_row_id(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    """Version, stage markers, link, supersede and clear are all anchored to the
    sweep's `ingestion_run.submit` row.
    """
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    store = rig.audit_store()

    _sweep(rig, _pipeline(rig), store, emitter)

    (opening_id, *_) = store.event_ids
    assert store.rows[0].action == "ingestion_run.submit"
    links = [*rig.audit_links("cra_native"), *rig.audit_links("policy_system")]
    assert len(links) == 8  # version, 4 markers, link, clear; the policy_system supersede
    assert set(links) == {opening_id}


def test_already_processed_runs_no_stage_and_writes_no_audit_pair(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    rig.seed_instrument(rig.native, _NEW)
    rig.seed_edge(rig.native, _PRIOR, _NEW, absorbed=True)
    rig.native.nodes[("RegulatoryInstrument", _PRIOR)]["status"] = "superseded"
    pipeline = _pipeline(rig)
    store = rig.audit_store()

    result = _sweep(rig, pipeline, store, emitter)

    assert pipeline.recorder.calls == []
    assert store.rows == []
    assert rig.rig.store.groups == []  # no group, so no group without an audit row
    assert [o.outcome for o in result.instruments] == ["amendment_reingested"]
    assert result.instruments[0].detail == f"{_NEW} (already_processed)"


def test_sweep_writes_the_succession_into_policy_system_so_the_prior_is_no_longer_active(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)

    _sweep(rig, _pipeline(rig), rig.audit_store(), emitter)

    assert rig.status_of(rig.single_tenant, _PRIOR) == "superseded"
    assert rig.status_of(rig.single_tenant, _NEW) == "active"
    assert rig.edge_properties(rig.single_tenant, _PRIOR, _NEW) == [
        {"identity": f"SUPERSEDED_BY:{_PRIOR}->{_NEW}"}
    ]


def test_sweep_finalizes_a_crashed_succession_without_stages_and_audits_it_as_a_pair(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    """The native link landed (edge + `linked` marker) but `policy_system` was never written.

    The `finalize` runs no stage but writes the succession, so it records the
    `ingestion_run.submit` row lazily, just before its first group, and the matching
    `.complete` row; both groups carry the submit row's id.
    """
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    rig.seed_instrument(rig.native, _NEW)
    rig.seed_edge(rig.native, _PRIOR, _NEW, absorbed=True, legacy=False)
    rig.native.nodes[("RegulatoryInstrument", _PRIOR)]["status"] = "superseded"
    rig.seed_marker(rig.native, _NEW, "linked")
    pipeline = _pipeline(rig)
    store = rig.audit_store()

    result = _sweep(rig, pipeline, store, emitter)

    assert pipeline.recorder.calls == []
    assert result.instruments[0].detail == f"{_NEW} (superseded, finalize)"
    assert result.instruments[0].reingest_run_id is None
    assert rig.status_of(rig.single_tenant, _PRIOR) == "superseded"
    assert rig.marker_stage(rig.native, _NEW) is None
    assert [row.action for row in store.rows] == ["ingestion_run.submit", "ingestion_run.complete"]
    assert store.rows[0].resource_id == store.rows[1].resource_id
    assert store.rows[1].details["status"] == "succeeded"
    assert store.rows[1].details["new_obligations"] == 0
    links = [*rig.audit_links("cra_native"), *rig.audit_links("policy_system")]
    assert links == [store.event_ids[0]] * 2  # the policy_system supersede, then the marker delete


def test_a_link_only_run_whose_audit_row_cannot_be_written_aborts_the_sweep_and_writes_nothing(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    rig.seed_instrument(rig.native, _NEW)
    rig.seed_marker(rig.native, _NEW, "merge")  # every stage done: only the succession is left
    store = rig.audit_store()
    store.fail_on_outcome["applied"] = AuditTrailUnavailableError("audit down")

    with pytest.raises(AuditTrailUnavailableError):
        _sweep(rig, _pipeline(rig), store, emitter)

    assert rig.rig.store.groups == []
    assert rig.status_of(rig.native, _PRIOR) == "active"
    assert rig.marker_stage(rig.native, _NEW) == "merge"


def test_a_pending_marker_group_fails_the_run_and_the_next_sweep_catches_up_before_it_reads(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    """The stage marker is committed to the log but not applied (FalkorDB went away).

    The first sweep fails closed (`graph_unavailable`); once FalkorDB is back, the next sweep
    applies the pending entries BEFORE it classifies the graph, so it resumes after the marked
    stage instead of re-running ingestion, with no manual catch-up.
    """
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    pipeline = _pipeline(rig)
    store = rig.audit_store()
    rig.native.fail_after_n_writes(1, redis.exceptions.ConnectionError("FalkorDB went away"))

    first = _sweep(rig, pipeline, store, emitter)

    assert [(o.outcome, o.detail) for o in first.instruments] == [
        ("reingest_failed", "graph_unavailable")
    ]
    assert rig.marker_stage(rig.native, _NEW) is None  # committed to the log, not applied
    calls_before = len(pipeline.recorder.calls)
    rig.native.heal()

    second = _sweep(rig, pipeline, store, emitter)

    assert [c.stage for c in pipeline.recorder.calls[calls_before:]] == [
        "extraction",
        "derivation",
        "merge",
    ]
    assert second.instruments[0].detail == f"{_NEW} (superseded, resume)"
    assert rig.status_of(rig.native, _PRIOR) == "superseded"
    assert rig.marker_stage(rig.native, _NEW) is None


def test_a_graph_that_cannot_catch_up_fails_the_instrument_before_anything_runs(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    pipeline = _pipeline(rig)
    store = rig.audit_store()
    rig.native.fail_after_n_writes(1, redis.exceptions.ConnectionError("FalkorDB went away"))
    _sweep(rig, pipeline, store, emitter)  # leaves the stage marker pending
    calls_before = len(pipeline.recorder.calls)
    groups_before = len(rig.rig.store.groups)

    still_down = _sweep(rig, pipeline, store, emitter)  # FalkorDB is still unreachable

    assert [(o.outcome, o.detail) for o in still_down.instruments] == [
        ("reingest_failed", "graph_unavailable")
    ]
    assert len(pipeline.recorder.calls) == calls_before
    assert len(rig.rig.store.groups) == groups_before
