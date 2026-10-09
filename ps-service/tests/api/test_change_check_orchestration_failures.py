"""Failure injection and retry for the `check_regulations` sweep's full pipeline (issue #201).

Entry is the real `run_change_check_sweep`; the REAL `trigger_reingestion` / `will_reingest` /
`classify_reingestion` / `succession` run over stateful ledger doubles for each instrument's
`{short}_native` graph and for `policy_system`. The four pipeline stages are the hand-written
`FakePipeline` seam with a one-shot injected failure (a transient fault: the retry succeeds).

Two tracked instruments, CRA first and GDPR second, prove the sweep continues past a failure.
The injected exception text carries a filesystem path and a `host:port` so a leak into the
sweep result, an audit row or a sweep log line is detectable.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import TYPE_CHECKING

import pytest
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
from ps_service.api.change_check_orchestration import ChangeCheckResult, run_change_check_sweep
from ps_service.audit import AuditContext
from ps_service.change_monitor.models import AmendmentFinding, PollReport, TrackedInstrumentNode
from ps_service.change_monitor.trigger import trigger_reingestion, will_reingest
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
    from api._fakes import FakePipeline, MakeEmitter, ReadLines
    from ps_service.api.ingestion_orchestration import GraphHandle
    from ps_service.ingestion.models import InstrumentType
    from ps_service.logging import LogEmitter

_ALL = ("ingestion", "extraction", "derivation", "merge")
_LEAKY_PATH = "/very/deep/absolute/filesystem/path/to/some/internal/module.py"
_LEAKY_HOST = "internal-host.example.com:6379"
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    llm_interface_model="azure/gpt-4o",
    llm_interface_embed_model="azure/text-embedding-3-large",
    company_merge_similarity_threshold=0.83,
)
# (short name, base-act CELEX, detected consolidated CELEX = the new version)
_CRA = ("CRA", "32024R2847", "32024R2847C01")
_GDPR = ("GDPR", "32016R0679", "32016R0679C01")


def _new_id(instrument: tuple[str, str, str]) -> str:
    return f"{instrument[0]}-{instrument[2]}"


def _prior_id(instrument: tuple[str, str, str]) -> str:
    return f"{instrument[0]}-1.0"


class _World:
    """Ledger doubles for each instrument's native graph and for `policy_system`."""

    def __init__(
        self,
        instruments: tuple[tuple[str, str, str], ...],
        **pipeline: object,
    ) -> None:
        self.instruments = instruments
        self.config = _CONFIG  # a test may swap in an incomplete config before `sweep`
        self.fetched_type: InstrumentType = "regulation"  # what the Cellar metadata fetch reports
        self.native = {short: LedgerNativeGraph() for short, _, _ in instruments}
        self.single_tenant = LedgerSingleTenantGraph()
        for instrument in instruments:
            self.native[instrument[0]].seed_instrument(_prior_id(instrument))
            self.single_tenant.seed_instrument(_prior_id(instrument))
        self.store = InMemoryAuditStore()
        self.pipeline: FakePipeline = build_fake_pipeline_dependencies(
            rid=None,
            recorder=LedgerStageRecorder(self.native[instruments[0][0]].events, self._on_ingest),
            **pipeline,  # pyright: ignore[reportArgumentType]
        )

    def _on_ingest(self, call: object) -> None:
        """The real Ingestion stage registers the new node; Company Merge later lands it."""
        kwargs = call.kwargs  # pyright: ignore[reportAttributeAccessIssue,reportUnknownVariableType,reportUnknownMemberType]
        short, version = str(kwargs["short_name"]), str(kwargs["version"])  # pyright: ignore[reportUnknownArgumentType]
        self.native[short].seed_instrument(f"{short}-{version}")
        self.single_tenant.seed_instrument(f"{short}-{version}")

    def sweep(self, emitter: LogEmitter) -> ChangeCheckResult:
        tracked = tuple(
            TrackedInstrumentNode(_prior_id(i), i[1], "regulation", "2024-01-01")
            for i in self.instruments
        )
        findings = tuple(
            AmendmentFinding(
                _prior_id(i),
                "regulation",
                "2024-01-01",
                i[2],
                date(2025, 1, 1),
                "newer_consolidation",
            )
            for i in self.instruments
        )
        fake = build_fake_change_check_dependencies(
            tracked=tracked,
            poll_report=PollReport(
                findings=findings, polled_count=len(tracked), failed_ids=(), unconfigured_ids=()
            ),
            catalog_entries={
                i[1]: CatalogEntry(celex=i[1], title=i[0], short_name=i[0], version="1.0")
                for i in self.instruments
            },
            reingestion_result=None,
            pipeline=self.pipeline,
        )

        def _open_native(_config: ServiceConfig, short_name: str) -> GraphHandle:
            return self.native[short_name]

        deps = dataclasses.replace(
            fake.dependencies,
            open_single_tenant=lambda _config: self.single_tenant,  # pyright: ignore[reportUnknownLambdaType]
            open_native=_open_native,
            default_adapter=lambda: FakeAdapter(
                {i[1]: _structure(self.fetched_type) for i in self.instruments}
            ),
            trigger_reingestion=trigger_reingestion,  # pyright: ignore[reportArgumentType]
            will_reingest=will_reingest,  # pyright: ignore[reportArgumentType]
        )
        return run_change_check_sweep(
            config=self.config,
            run_id="sweep",
            dependencies=deps,
            audit=AuditContext(("caller", "https://issuer.example.com/"), self.store),
            emitter=emitter,
        )


def _leaky_error() -> RuntimeError:
    return RuntimeError(f"connection to {_LEAKY_HOST} failed while reading {_LEAKY_PATH}")


@pytest.mark.parametrize("stage", _ALL)
def test_stage_failure_keeps_prior_active_reports_enumerated_reason_and_sweep_continues(
    stage: str, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    world = _World((_CRA, _GDPR), errors_once=True, **{f"{_FIELD[stage]}_error": _leaky_error()})

    result = world.sweep(emitter)
    emitter.flush()

    failed, absorbed = result.instruments
    # (a) the failed instrument: no edge, prior still active (native and policy_system)
    assert failed.outcome == "reingest_failed"
    assert failed.detail == f"pipeline_stage_failed (stage: {stage})"
    cra = world.native["CRA"]
    assert [key for key in cra.edges if key[0] == "SUPERSEDED_BY"] == []
    assert cra.status_of(_prior_id(_CRA)) == "active"
    assert world.single_tenant.status_of(_prior_id(_CRA)) == "active"
    # (b) the audit completion row carries the enumerated reason code, never the text
    submit, complete = world.store.rows[0], world.store.rows[1]
    assert submit.action == "ingestion_run.submit"
    assert complete.details["status"] == "failed"
    assert complete.details["reason_code"] == "pipeline_stage_failed"
    # (c) the sweep continued: the next instrument was absorbed
    assert absorbed.outcome == "amendment_reingested"
    assert absorbed.detail == f"{_new_id(_GDPR)} (superseded, fresh)"
    assert world.native["GDPR"].status_of(_prior_id(_GDPR)) == "superseded"
    assert world.single_tenant.status_of(_prior_id(_GDPR)) == "superseded"
    assert [r.details["status"] for r in world.store.rows if r.action.endswith("complete")] == [
        "failed",
        "succeeded",
    ]
    # (d) no raw exception text reaches the result, the audit rows or the sweep's own log lines
    sweep_lines = [
        line
        for line in read_lines(log_path)
        if line.get("action") in {"change_check_sweep", "change_check_instrument"}
    ]
    for surface in (repr(result), repr(world.store.rows), repr(sweep_lines)):
        assert _LEAKY_PATH not in surface
        assert _LEAKY_HOST not in surface
    failing_line = next(
        line
        for line in sweep_lines
        if line.get("entity_id") == _prior_id(_CRA) and line.get("outcome") == "reingest_failed"
    )
    assert failing_line["reason_code"] == "pipeline_stage_failed"
    assert failing_line["failing_stage"] == stage


_FIELD = {
    "ingestion": "ingest",
    "extraction": "extract",
    "derivation": "derive",
    "merge": "merge",
}


@pytest.mark.parametrize(
    ("failing_stage", "stages_on_retry"),
    [
        ("extraction", ["extraction", "derivation", "merge"]),
        ("derivation", ["derivation", "merge"]),
        ("merge", ["merge"]),
    ],
)
def test_retry_after_a_stage_failure_reruns_only_the_missing_stages_then_links(
    failing_stage: str, stages_on_retry: list[str], make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    world = _World((_CRA,), errors_once=True, **{f"{_FIELD[failing_stage]}_error": _leaky_error()})
    first = world.sweep(emitter)
    assert first.instruments[0].outcome == "reingest_failed"
    calls_before = len(world.pipeline.recorder.calls)
    cra = world.native["CRA"]
    nodes_before = set(cra.nodes) - {("ReingestProgress", _new_id(_CRA))}

    second = world.sweep(emitter)

    retried = [call.stage for call in world.pipeline.recorder.calls[calls_before:]]
    assert retried == stages_on_retry  # ingestion never re-runs; only the missing stages do
    [outcome] = second.instruments
    assert outcome.outcome == "amendment_reingested"
    assert outcome.detail == f"{_new_id(_CRA)} (superseded, resume)"
    assert cra.edges[("SUPERSEDED_BY", _prior_id(_CRA), _new_id(_CRA))] == {"absorbed": True}
    assert cra.status_of(_prior_id(_CRA)) == "superseded"
    assert cra.marker_stage(_new_id(_CRA)) is None
    assert world.single_tenant.status_of(_prior_id(_CRA)) == "superseded"
    # no duplicate instrument nodes: the retry adds no node the first run did not create
    assert set(cra.nodes) == nodes_before
    # the retry that ran stages is audited as its own pair; the first attempt stays `failed`
    assert [r.details["status"] for r in world.store.rows if r.action.endswith("complete")] == [
        "failed",
        "succeeded",
    ]


def test_a_second_sweep_after_a_completed_retry_runs_nothing(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    world = _World((_CRA,), errors_once=True, derive_error=_leaky_error())
    world.sweep(emitter)
    world.sweep(emitter)
    calls_before = len(world.pipeline.recorder.calls)
    rows_before = len(world.store.rows)

    # the prior is now `superseded` in policy_system, so the real tracked set would not list
    # it; the fake poll still reports it, which exercises the `already_processed` guard.
    third = world.sweep(emitter)

    assert len(world.pipeline.recorder.calls) == calls_before
    assert len(world.store.rows) == rows_before
    assert third.instruments[0].detail == f"{_new_id(_CRA)} (already_processed)"


# --- AC-BI-006: incomplete pipeline config fails before any graph write ---


def _writes(world: _World, short: str) -> list[object]:
    """Every mutating statement the instrument's graphs received (`policy_system` is shared)."""
    return [
        *world.native[short].writes,
        *(
            call
            for call in world.single_tenant.writes
            if str((call.params or {}).get("prior_id", "")).startswith(f"{short}-")
        ),
        *world.pipeline.native.calls,
        *world.pipeline.baseline.calls,
        *world.pipeline.single_tenant.calls,
    ]


@pytest.mark.parametrize(
    "unset",
    ["llm_interface_model", "llm_interface_embed_model", "company_merge_similarity_threshold"],
)
def test_missing_pipeline_config_reports_reingest_failed_before_any_graph_write(
    unset: str, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    world = _World((_CRA, _GDPR))
    world.config = dataclasses.replace(_CONFIG, **{unset: None})
    # GDPR was fully absorbed by an earlier sweep: it needs no stage, so no config either.
    gdpr = world.native["GDPR"]
    gdpr.seed_instrument(_new_id(_GDPR))
    gdpr.seed_edge(_prior_id(_GDPR), _new_id(_GDPR), absorbed=True)
    gdpr.nodes[("RegulatoryInstrument", _prior_id(_GDPR))]["status"] = "superseded"

    result = world.sweep(emitter)

    failed, already = result.instruments
    assert failed.outcome == "reingest_failed"
    assert failed.detail == "config_incomplete"
    assert _writes(world, "CRA") == []
    assert world.pipeline.recorder.calls == []
    assert world.native["CRA"].status_of(_prior_id(_CRA)) == "active"
    submit, complete = world.store.rows
    assert submit.action == "ingestion_run.submit"
    assert complete.details["status"] == "failed"
    assert complete.details["reason_code"] == "config_incomplete"
    assert already.outcome == "amendment_reingested"
    assert already.detail == f"{_new_id(_GDPR)} (already_processed)"


# --- AC-BI-005: national_transposition is skipped and nothing is written by any stage ---


@pytest.mark.parametrize("limb", ["prior_type", "fetched_metadata"])
@pytest.mark.parametrize("config_state", ["set", "unset"])
def test_national_transposition_is_skipped_and_nothing_is_written_including_stages(
    limb: str, config_state: str, make_emitter: MakeEmitter
) -> None:
    """The guard runs before the config check, so an unset model never masks the skip."""
    emitter, _ = make_emitter()
    world = _World((_CRA,))
    if config_state == "unset":
        world.config = dataclasses.replace(_CONFIG, llm_interface_model=None)
    if limb == "prior_type":
        world.native["CRA"].seed_instrument(
            _prior_id(_CRA), instrument_type="national_transposition"
        )
    else:  # the fetched-metadata limb: an ordinary prior whose amendment reports a different type
        world.fetched_type = "national_transposition"

    result = world.sweep(emitter)

    skipped = result.instruments[0]
    assert skipped.outcome == "skipped"
    assert skipped.detail is not None
    assert "national_transposition" in skipped.detail
    assert _writes(world, "CRA") == []
    assert [call.stage for call in world.pipeline.recorder.calls] == []
    assert world.native["CRA"].status_of(_prior_id(_CRA)) == "active"
    assert world.single_tenant.status_of(_prior_id(_CRA)) == "active"
    complete = next(r for r in world.store.rows if r.action.endswith("complete"))
    assert complete.details["status"] == "failed"
    assert complete.details["reason_code"] == "unsupported_instrument_type"
