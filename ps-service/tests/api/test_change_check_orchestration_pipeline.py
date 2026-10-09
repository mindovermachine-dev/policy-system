"""The `check_regulations` sweep runs the full UC-4 pipeline, succession last (issue #201).

Entry is the real `run_change_check_sweep`; the code under test includes the REAL
`trigger_reingestion` / `will_reingest` / `classify_reingestion` / `succession`. The boundaries
are doubles: the `{short}_native` graph is the stateful `LedgerNativeGraph`, the four pipeline
stages are the hand-written `FakePipeline` stage seam (recording into a `LedgerStageRecorder`,
so stage calls and native-graph writes land on ONE ordered timeline).
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import TYPE_CHECKING

from change_monitor._fakes import FakeAdapter, LedgerNativeGraph, LedgerSingleTenantGraph
from change_monitor.test_trigger import (
    _structure,  # pyright: ignore[reportPrivateUsage]  -- reuse the trigger tests' canned metadata
)

from api._audit_fakes import InMemoryAuditStore
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
from ps_service.change_monitor.models import AmendmentFinding, PollReport, TrackedInstrumentNode
from ps_service.change_monitor.trigger import trigger_reingestion, will_reingest
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
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
    company_merge_similarity_threshold=0.83,
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


def _ledger_with_prior() -> LedgerNativeGraph:
    ledger = LedgerNativeGraph()
    ledger.seed_instrument(_PRIOR)
    return ledger


def _single_tenant(ledger: LedgerNativeGraph) -> LedgerSingleTenantGraph:
    single_tenant = LedgerSingleTenantGraph(ledger.events)
    single_tenant.seed_instrument(_PRIOR)
    single_tenant.seed_instrument(_NEW)
    return single_tenant


def _pipeline(ledger: LedgerNativeGraph, **kwargs: object) -> FakePipeline:
    recorder = LedgerStageRecorder(
        ledger.events, on_ingest=lambda _call: ledger.seed_instrument(_NEW)
    )
    return build_fake_pipeline_dependencies(recorder=recorder, rid=_NEW, **kwargs)  # pyright: ignore[reportArgumentType]


def _sweep(
    ledger: LedgerNativeGraph,
    pipeline: FakePipeline,
    store: InMemoryAuditStore,
    emitter: object,
    *,
    single_tenant: LedgerSingleTenantGraph | None = None,
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
        open_single_tenant=lambda _config: single_tenant or _single_tenant(ledger),  # pyright: ignore[reportUnknownLambdaType]
        open_native=lambda _config, _short: ledger,  # pyright: ignore[reportUnknownLambdaType]
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
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger_with_prior()
    pipeline = _pipeline(ledger)

    result = _sweep(ledger, pipeline, InMemoryAuditStore(), emitter)

    assert pipeline.recorder.order == list(_ALL)
    stage_and_link = [e for e in ledger.events if e.startswith("stage:") or "SUPERSEDED_BY" in e]
    assert stage_and_link == [f"stage:{stage}" for stage in _ALL] + [
        "write:SUPERSEDED_BY",
        "write:policy_system:SUPERSEDED_BY",
    ]
    # nothing may be superseded before the merge stage has been called
    assert ledger.events.index("write:SUPERSEDED_BY") > ledger.events.index("stage:merge")
    [outcome] = result.instruments
    assert outcome.outcome == "amendment_reingested"
    assert outcome.detail == f"{_NEW} (superseded, fresh)"
    assert ledger.edges[("SUPERSEDED_BY", _PRIOR, _NEW)] == {"absorbed": True}
    assert ledger.status_of(_PRIOR) == "superseded"
    assert ledger.marker_stage(_NEW) is None


def test_the_audit_pair_brackets_the_run_and_shares_its_run_id_with_the_stages(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger_with_prior()
    pipeline = _pipeline(ledger)
    store = InMemoryAuditStore()

    result = _sweep(ledger, pipeline, store, emitter)

    [outcome] = result.instruments
    assert [row.action for row in store.rows] == ["ingestion_run.submit", "ingestion_run.complete"]
    assert outcome.reingest_run_id == store.rows[0].resource_id == store.rows[1].resource_id
    ingest_call = pipeline.recorder.calls[0]
    assert ingest_call.kwargs["run_id"] == outcome.reingest_run_id
    assert ingest_call.kwargs["version"] == _NEW_CELEX


def test_already_processed_runs_no_stage_and_writes_no_audit_pair(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger_with_prior()
    ledger.seed_instrument(_NEW)
    ledger.seed_edge(_PRIOR, _NEW, absorbed=True)
    ledger.nodes[("RegulatoryInstrument", _PRIOR)]["status"] = "superseded"
    pipeline = _pipeline(ledger)
    store = InMemoryAuditStore()

    result = _sweep(ledger, pipeline, store, emitter)

    assert pipeline.recorder.calls == []
    assert store.rows == []
    assert ledger.writes == []
    assert [o.outcome for o in result.instruments] == ["amendment_reingested"]
    assert result.instruments[0].detail == f"{_NEW} (already_processed)"


def test_sweep_writes_the_succession_into_policy_system_so_the_prior_is_no_longer_active(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger_with_prior()
    single_tenant = _single_tenant(ledger)

    _sweep(ledger, _pipeline(ledger), InMemoryAuditStore(), emitter, single_tenant=single_tenant)

    assert single_tenant.status_of(_PRIOR) == "superseded"
    assert single_tenant.status_of(_NEW) == "active"
    assert single_tenant.edges == {(_PRIOR, _NEW)}


def test_sweep_finalizes_a_crashed_succession_without_stages_or_an_audit_pair(
    make_emitter: MakeEmitter,
) -> None:
    """The native link landed (edge + `linked` marker) but `policy_system` was never written."""
    emitter, _ = make_emitter()
    ledger = _ledger_with_prior()
    ledger.seed_instrument(_NEW)
    ledger.seed_edge(_PRIOR, _NEW, absorbed=True)
    ledger.nodes[("RegulatoryInstrument", _PRIOR)]["status"] = "superseded"
    ledger.seed_marker(_NEW, "linked")
    single_tenant = _single_tenant(ledger)
    pipeline = _pipeline(ledger)
    store = InMemoryAuditStore()

    result = _sweep(ledger, pipeline, store, emitter, single_tenant=single_tenant)

    assert pipeline.recorder.calls == []
    assert store.rows == []
    assert result.instruments[0].detail == f"{_NEW} (superseded, finalize)"
    assert single_tenant.status_of(_PRIOR) == "superseded"
    assert ledger.marker_stage(_NEW) is None
