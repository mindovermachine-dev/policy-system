"""Tests for `ps_service.change_monitor.trigger.trigger_reingestion` and `will_reingest`.

Issue #201: a detected amendment runs the full UC-4 pipeline (Ingestion -> Domain Mapper ->
Company Merge) through an injected `PipelineRunner`, and the succession (`SUPERSEDED_BY`,
`prior.status='superseded'`, `absorbed`) is the LAST write, only after the merge stage
returned. The `{short}_native` graph is the stateful `LedgerNativeGraph` boundary double; the
runner is the hand-written `RecordingRunner` seam double. The code under test (`trigger`,
`succession`, `classify_reingestion`) is real.

The `national_transposition` guard (AC-010) runs before any stage or write for every state
that has stages to run: its prior-type limb always, its metadata-fetch limb on `fresh` only.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from change_monitor._fakes import (
    FakeAdapter,
    LedgerNativeGraph,
    LedgerSingleTenantGraph,
    MakeEmitter,
    ReadLines,
    RecordingRunner,
)
from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    NationalTranspositionNotSupportedError,
)
from ps_service.change_monitor.trigger import trigger_reingestion, will_reingest
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    InstrumentType,
    RegulatoryInstrumentMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.change_monitor.models import ReingestionOutcome

_IDENTIFIER = "32024R2847"
_PRIOR_ID = "CRA-1.0"
_NEW_ID = "CRA-2.0"
_ALL = ("ingestion", "extraction", "derivation", "merge")


def _structure(
    instrument_type: InstrumentType = "regulation",
) -> FetchedRegulatoryInstrumentStructure:
    """A canned structure whose metadata carries `instrument_type` (the guard's metadata fetch)."""
    metadata = RegulatoryInstrumentMetadata(
        title="Fixture",
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external",
        instrument_type=instrument_type,
    )
    return FetchedRegulatoryInstrumentStructure(metadata=metadata, nodes=(), edges=())


def _ledger(
    *,
    prior_type: str = "regulation",
    new_node: bool = False,
    marker: str | None = None,
    linked_absorbed: bool | str | None = "no-edge",
) -> LedgerNativeGraph:
    """The `{short}_native` state: an active prior, optionally the new node / marker / edge."""
    ledger = LedgerNativeGraph()
    has_edge = linked_absorbed != "no-edge"
    ledger.seed_instrument(
        _PRIOR_ID, status="superseded" if has_edge else "active", instrument_type=prior_type
    )
    if new_node or has_edge:
        ledger.seed_instrument(_NEW_ID)
    if has_edge:
        ledger.seed_edge(_PRIOR_ID, _NEW_ID, absorbed=linked_absorbed)  # pyright: ignore[reportArgumentType]
    if marker is not None:
        ledger.seed_marker(_NEW_ID, marker)
    return ledger


def _single_tenant(ledger: LedgerNativeGraph) -> LedgerSingleTenantGraph:
    """A `policy_system` double holding both instruments as active, on the ledger's timeline."""
    single_tenant = LedgerSingleTenantGraph(ledger.events)
    single_tenant.seed_instrument(_PRIOR_ID)
    single_tenant.seed_instrument(_NEW_ID)
    return single_tenant


def _trigger(
    ledger: LedgerNativeGraph,
    runner: RecordingRunner | None,
    emitter: object,
    *,
    single_tenant: LedgerSingleTenantGraph | None = None,
    adapter: FakeAdapter | None = None,
    run_id: str | None = None,
) -> ReingestionOutcome:
    return trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=adapter or FakeAdapter({_IDENTIFIER: _structure()}),
        graph=ledger,
        single_tenant=single_tenant or _single_tenant(ledger),
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        run_id=run_id,
        run_pipeline=runner,
    )


# --- fresh: all four stages in order, succession is the LAST write (AC-BI-001, AC-BI-003) ---


@pytest.mark.parametrize("prior_instrument_type", ["regulation", "directive"])
def test_fresh_runs_all_four_stages_then_writes_succession_last(
    prior_instrument_type: str, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger(prior_type=prior_instrument_type)
    runner = RecordingRunner(ledger, _NEW_ID)

    outcome = _trigger(ledger, runner, emitter, run_id="run-1")

    assert [call.stages for call in runner.calls] == [_ALL]
    assert ledger.events == [
        "stage:ingestion",
        "write:version",
        "write:marker:ingestion",
        "stage:extraction",
        "write:marker:extraction",
        "stage:derivation",
        "write:marker:derivation",
        "stage:merge",
        "write:marker:merge",
        "write:SUPERSEDED_BY",
        "write:policy_system:SUPERSEDED_BY",
        "write:clear_marker",
    ]
    assert outcome.outcome == "superseded"
    assert outcome.state == "fresh"
    assert outcome.run_id == "run-1"
    assert outcome.prior_regulatory_instrument_id == _PRIOR_ID
    assert outcome.new_regulatory_instrument_id == _NEW_ID
    assert [summary.stage for summary in outcome.stage_summaries] == list(_ALL)


def test_fresh_ends_with_a_completed_succession_and_no_marker(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger()

    _trigger(ledger, RecordingRunner(ledger, _NEW_ID), emitter)

    assert ledger.edges[("SUPERSEDED_BY", _PRIOR_ID, _NEW_ID)] == {"absorbed": True}
    assert ledger.status_of(_PRIOR_ID) == "superseded"
    assert ledger.marker_stage(_NEW_ID) is None
    assert ledger.nodes[("RegulatoryInstrument", _NEW_ID)]["version"] == "2.0"


def test_fresh_mints_a_run_id_when_the_caller_gives_none(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger()
    runner = RecordingRunner(ledger, _NEW_ID)

    outcome = _trigger(ledger, runner, emitter)

    assert outcome.run_id
    assert runner.calls[0].run_id == outcome.run_id


def test_fresh_guard_uses_the_metadata_fetch_only(make_emitter: MakeEmitter) -> None:
    """AC-BI-004/011: the guard fetches metadata only; only the stages fetch structure."""
    emitter, _ = make_emitter()
    ledger = _ledger()
    adapter = FakeAdapter({_IDENTIFIER: _structure("regulation")})

    _trigger(ledger, RecordingRunner(ledger, _NEW_ID), emitter, adapter=adapter)

    assert adapter.metadata_calls == [_IDENTIFIER]
    assert adapter.structure_calls == []


def test_fresh_emits_one_link_entry_and_one_entry_per_completed_stage(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    ledger = _ledger()

    outcome = _trigger(ledger, RecordingRunner(ledger, _NEW_ID), emitter, run_id="run-1")
    emitter.flush()

    lines = read_lines(log_path)
    [classified] = [line for line in lines if line["action"] == "classify_reingestion"]
    assert classified["outcome"] == "fresh"
    stage_entries = [line for line in lines if line["action"] == "run_pipeline_stage"]
    assert [(line["entity_id"], line["stage"], line["outcome"]) for line in stage_entries] == [
        (_NEW_ID, stage, "succeeded") for stage in _ALL
    ]
    [link] = [line for line in lines if line["action"] == "link_superseded_by"]
    assert link["component"] == "change_monitor"
    assert link["entity_id"] == [_PRIOR_ID, _NEW_ID]
    assert link["outcome"] == "superseded"
    assert link["run_id"] == outcome.run_id == "run-1"


# --- failure and retry (AC-BI-007, AC-BI-008) ---


@pytest.mark.parametrize("failing_stage", _ALL)
def test_a_failed_stage_propagates_and_writes_no_succession(
    failing_stage: str, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger()

    with pytest.raises(RuntimeError, match=failing_stage):
        _trigger(ledger, RecordingRunner(ledger, _NEW_ID, fail_at=failing_stage), emitter)

    assert ("SUPERSEDED_BY", _PRIOR_ID, _NEW_ID) not in ledger.edges
    assert ledger.status_of(_PRIOR_ID) == "active"
    completed = _ALL[: _ALL.index(failing_stage)]
    assert ledger.marker_stage(_NEW_ID) == (completed[-1] if completed else None)


def test_a_retry_after_a_derive_failure_reruns_only_derive_and_merge(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger()
    with pytest.raises(RuntimeError):
        _trigger(ledger, RecordingRunner(ledger, _NEW_ID, fail_at="derivation"), emitter)
    retry = RecordingRunner(ledger, _NEW_ID)

    outcome = _trigger(ledger, retry, emitter)

    assert [call.stages for call in retry.calls] == [("derivation", "merge")]
    assert outcome.state == "resume"
    assert outcome.outcome == "superseded"
    assert ledger.edges[("SUPERSEDED_BY", _PRIOR_ID, _NEW_ID)] == {"absorbed": True}
    assert ledger.marker_stage(_NEW_ID) is None


def test_a_crash_between_merge_and_link_links_without_running_a_stage(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    ledger = _ledger(new_node=True, marker="merge")
    runner = RecordingRunner(ledger, _NEW_ID)

    outcome = _trigger(ledger, runner, emitter)
    emitter.flush()

    assert runner.calls == []
    assert outcome.outcome == "superseded"
    assert outcome.run_id is None
    assert outcome.stage_summaries == ()
    assert ledger.events == [
        "write:SUPERSEDED_BY",
        "write:policy_system:SUPERSEDED_BY",
        "write:clear_marker",
    ]
    assert [line["action"] for line in read_lines(log_path)] == [
        "classify_reingestion",
        "link_superseded_by",
    ]


def test_resume_without_a_marker_reruns_all_four_stages(make_emitter: MakeEmitter) -> None:
    """A partial ingest leaves the node behind; "node exists" must not mean "ingest finished"."""
    emitter, _ = make_emitter()
    ledger = _ledger(new_node=True)
    runner = RecordingRunner(ledger, _NEW_ID)

    _trigger(ledger, runner, emitter)

    assert [call.stages for call in runner.calls] == [_ALL]


def test_stages_to_run_without_a_runner_is_an_inconsistent_call_and_writes_nothing(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger()

    with pytest.raises(ChangeMonitorStateError):
        _trigger(ledger, None, emitter)

    assert ledger.writes == []


# --- D2: succession is visible in `policy_system`, in three idempotent steps ---


def test_a_completed_succession_is_written_into_policy_system_too(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger()
    single_tenant = _single_tenant(ledger)

    _trigger(ledger, RecordingRunner(ledger, _NEW_ID), emitter, single_tenant=single_tenant)

    assert single_tenant.status_of(_PRIOR_ID) == "superseded"
    assert single_tenant.status_of(_NEW_ID) == "active"
    assert single_tenant.edges == {(_PRIOR_ID, _NEW_ID)}


def test_a_failed_stage_leaves_policy_system_untouched(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger()
    single_tenant = _single_tenant(ledger)

    with pytest.raises(RuntimeError):
        _trigger(
            ledger,
            RecordingRunner(ledger, _NEW_ID, fail_at="merge"),
            emitter,
            single_tenant=single_tenant,
        )

    assert single_tenant.writes == []
    assert single_tenant.status_of(_PRIOR_ID) == "active"


def test_a_crash_after_the_native_link_finalizes_on_the_next_call(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Step 1 (native edge + `linked` marker) landed, step 2 (`policy_system`) did not."""
    emitter, log_path = make_emitter()
    ledger = _ledger(linked_absorbed=True, marker="linked")
    single_tenant = _single_tenant(ledger)
    runner = RecordingRunner(ledger, _NEW_ID)

    outcome = _trigger(ledger, runner, emitter, single_tenant=single_tenant)
    emitter.flush()

    assert runner.calls == []
    assert (outcome.outcome, outcome.state, outcome.run_id) == ("superseded", "finalize", None)
    assert single_tenant.status_of(_PRIOR_ID) == "superseded"
    assert single_tenant.edges == {(_PRIOR_ID, _NEW_ID)}
    assert ledger.marker_stage(_NEW_ID) is None
    assert [line["action"] for line in read_lines(log_path)] == [
        "classify_reingestion",
        "link_superseded_by",
    ]


def test_after_finalize_the_next_call_is_already_processed(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger(linked_absorbed=True, marker="linked")
    single_tenant = _single_tenant(ledger)
    _trigger(ledger, None, emitter, single_tenant=single_tenant)

    again = _trigger(ledger, None, emitter, single_tenant=single_tenant)

    assert again.outcome == "already_processed"


def test_a_missing_prior_in_policy_system_is_an_inconsistent_graph_and_keeps_the_marker(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger(linked_absorbed=True, marker="linked")
    empty_single_tenant = LedgerSingleTenantGraph(ledger.events)

    with pytest.raises(ChangeMonitorStateError, match=_PRIOR_ID):
        _trigger(ledger, None, emitter, single_tenant=empty_single_tenant)

    assert ledger.marker_stage(_NEW_ID) == "linked"  # still finalizable on the next sweep


# --- already_processed (AC-BI-009) ---


def test_already_processed_runs_no_stage_writes_nothing_and_logs_no_link(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    ledger = _ledger(linked_absorbed=True)
    runner = RecordingRunner(ledger, _NEW_ID)
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    outcome = _trigger(ledger, runner, emitter, adapter=adapter)
    emitter.flush()

    assert outcome.outcome == "already_processed"
    assert outcome.state == "already_processed"
    assert outcome.run_id is None
    assert outcome.stage_summaries == ()
    assert outcome.prior_regulatory_instrument_id == _PRIOR_ID
    assert runner.calls == []
    assert adapter.calls == []
    assert ledger.writes == []
    assert [line["action"] for line in read_lines(log_path)] == ["classify_reingestion"]


# --- national_transposition guard (AC-010 / AC-BI-005) ---


@pytest.mark.parametrize(
    "ledger_factory",
    [
        pytest.param(lambda: _ledger(prior_type="national_transposition"), id="fresh"),
        pytest.param(
            lambda: _ledger(
                prior_type="national_transposition", new_node=True, marker="extraction"
            ),
            id="resume",
        ),
        pytest.param(
            lambda: _ledger(prior_type="national_transposition", linked_absorbed=None),
            id="repair",
        ),
    ],
)
def test_a_national_transposition_prior_is_rejected_before_any_stage_or_write(
    ledger_factory: Callable[[], LedgerNativeGraph], make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    ledger = ledger_factory()
    runner = RecordingRunner(ledger, _NEW_ID)
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    with pytest.raises(NationalTranspositionNotSupportedError) as excinfo:
        _trigger(ledger, runner, emitter, adapter=adapter)

    assert "#41" in str(excinfo.value)
    assert runner.calls == []
    assert ledger.writes == []
    assert adapter.calls == []  # limb 1 fires before the metadata fetch; resume/repair never fetch


def test_fetched_national_transposition_metadata_is_rejected_on_fresh(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger()
    runner = RecordingRunner(ledger, _NEW_ID)
    adapter = FakeAdapter({_IDENTIFIER: _structure("national_transposition")})

    with pytest.raises(NationalTranspositionNotSupportedError):
        _trigger(ledger, runner, emitter, adapter=adapter)

    assert adapter.metadata_calls == [_IDENTIFIER]
    assert runner.calls == []
    assert ledger.writes == []


def test_resume_makes_no_adapter_call(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    ledger = _ledger(new_node=True, marker="ingestion")
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    _trigger(ledger, RecordingRunner(ledger, _NEW_ID), emitter, adapter=adapter)

    assert adapter.calls == []


# --- the read-only `will_reingest` probe: "at least one stage will run" ---


@pytest.mark.parametrize(
    ("ledger", "expected"),
    [
        pytest.param(_ledger(), True, id="fresh"),
        pytest.param(_ledger(new_node=True, marker="extraction"), True, id="resume-with-stages"),
        pytest.param(_ledger(new_node=True, marker="merge"), False, id="link-only"),
        pytest.param(_ledger(linked_absorbed=True), False, id="already_processed"),
        pytest.param(_ledger(linked_absorbed=None), True, id="repair"),
    ],
)
def test_will_reingest_is_true_exactly_when_a_stage_will_run_and_writes_nothing(
    ledger: LedgerNativeGraph, *, expected: bool
) -> None:
    assert will_reingest(ledger, "CRA", "2.0") is expected
    assert ledger.writes == []


def test_will_reingest_raises_when_the_graph_has_no_single_active_prior() -> None:
    with pytest.raises(ChangeMonitorStateError):
        will_reingest(LedgerNativeGraph(), "CRA", "2.0")
