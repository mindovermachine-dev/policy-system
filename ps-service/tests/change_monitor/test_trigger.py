"""Tests for `ps_service.change_monitor.trigger.trigger_reingestion` and `will_reingest`.

Issue #201: a detected amendment runs the full UC-4 pipeline (Ingestion -> Domain Mapper ->
Company Merge) through an injected `PipelineRunner`, and the succession (`SUPERSEDED_BY`,
`prior.status='superseded'`, `absorbed`) is the LAST write, only after the merge stage
returned. Issue #213: every write is a Graph Write Gateway group; the `{short}_native` and
`policy_system` graphs are the process gateway rig's in-memory graphs (the true FalkorDB and
Postgres boundaries), read through `SuccessionReadGraph`, and the runner is the hand-written
`RigRunner` seam double. The code under test (`trigger`, `succession`, `classify_reingestion`) is
real, and the assertions are on the rig graphs and its log.

The `national_transposition` guard (AC-010) runs before any stage or write for every state
that has stages to run: its prior-type limb always, its metadata-fetch limb on `fresh` only.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from change_monitor._fakes import FakeAdapter, MakeEmitter, ReadLines
from change_monitor._gateway_fakes import RigRunner
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
    from change_monitor._gateway_fakes import SuccessionRig
    from ps_service.change_monitor.models import ReingestionOutcome

_IDENTIFIER = "32024R2847"
_PRIOR_ID = "CRA-1.0"
_NEW_ID = "CRA-2.0"
_ALL = ("ingestion", "extraction", "derivation", "merge")
_EDGE_IDENTITY = f"SUPERSEDED_BY:{_PRIOR_ID}->{_NEW_ID}"


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


def _world(
    rig: SuccessionRig,
    *,
    prior_type: str = "regulation",
    new_node: bool = False,
    marker: str | None = None,
    linked_absorbed: bool | str | None = "no-edge",
    legacy_edge: bool = True,
) -> SuccessionRig:
    """The `{short}_native` state: an active prior, optionally the new node / marker / edge.

    `policy_system` holds both instruments as active (Company Merge landed them). An edge is
    seeded as the pre-gateway code left it (without an `identity`) unless `legacy_edge` is false.
    """
    has_edge = linked_absorbed != "no-edge"
    rig.seed_instrument(
        rig.native,
        _PRIOR_ID,
        status="superseded" if has_edge else "active",
        instrument_type=prior_type,
    )
    if new_node or has_edge:
        rig.seed_instrument(rig.native, _NEW_ID)
    if has_edge:
        absorbed = None if linked_absorbed is None else bool(linked_absorbed)
        rig.seed_edge(rig.native, _PRIOR_ID, _NEW_ID, absorbed=absorbed, legacy=legacy_edge)
    if marker is not None:
        rig.seed_marker(rig.native, _NEW_ID, marker)
    rig.seed_instrument(rig.single_tenant, _PRIOR_ID)
    rig.seed_instrument(rig.single_tenant, _NEW_ID)
    return rig


def _trigger(
    rig: SuccessionRig,
    runner: RigRunner | None,
    emitter: object,
    *,
    adapter: FakeAdapter | None = None,
    run_id: str | None = None,
) -> ReingestionOutcome:
    audit_event_id = rig.new_audit_event_id()
    return trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=adapter or FakeAdapter({_IDENTIFIER: _structure()}),
        graph=rig.read_native(),
        submitter=rig.rig.gateway,
        audit_anchor=lambda: audit_event_id,
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        run_id=run_id,
        run_pipeline=runner,
    )


# --- fresh: all four stages in order, succession is the LAST write (AC-BI-001, AC-BI-003) ---


@pytest.mark.parametrize("prior_instrument_type", ["regulation", "directive"])
def test_fresh_runs_all_four_stages_then_writes_succession_last(
    prior_instrument_type: str, succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig, prior_type=prior_instrument_type)
    runner = RigRunner(rig, _NEW_ID)

    outcome = _trigger(rig, runner, emitter, run_id="run-1")

    assert runner.calls == [_ALL]
    assert rig.timeline() == [
        "stage:ingestion",
        "group:cra_native:RegulatoryInstrument",  # the version
        "group:cra_native:ReingestProgress",
        "stage:extraction",
        "group:cra_native:ReingestProgress",
        "stage:derivation",
        "group:cra_native:ReingestProgress",
        "stage:merge",
        "group:cra_native:ReingestProgress",
        "group:cra_native:SUPERSEDED_BY",  # the link: edge, absorbed, status, marker
        "group:policy_system:SUPERSEDED_BY",
        "group:cra_native:ReingestProgress",  # the marker delete
    ]
    assert outcome.outcome == "superseded"
    assert outcome.state == "fresh"
    assert outcome.run_id == "run-1"
    assert outcome.prior_regulatory_instrument_id == _PRIOR_ID
    assert outcome.new_regulatory_instrument_id == _NEW_ID
    assert [summary.stage for summary in outcome.stage_summaries] == list(_ALL)


def test_fresh_ends_with_a_completed_succession_and_no_marker(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)

    _trigger(rig, RigRunner(rig, _NEW_ID), emitter)

    assert rig.edge_properties(rig.native, _PRIOR_ID, _NEW_ID) == [
        {"identity": _EDGE_IDENTITY, "absorbed": True}
    ]
    assert rig.status_of(rig.native, _PRIOR_ID) == "superseded"
    assert rig.marker_stage(rig.native, _NEW_ID) is None
    assert rig.version_of(rig.native, _NEW_ID) == "2.0"


def test_fresh_mints_a_run_id_when_the_caller_gives_none(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    runner = RigRunner(rig, _NEW_ID)

    outcome = _trigger(rig, runner, emitter)

    assert outcome.run_id
    assert runner.run_ids[0] == outcome.run_id


def test_fresh_guard_uses_the_metadata_fetch_only(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    """AC-BI-004/011: the guard fetches metadata only; only the stages fetch structure."""
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    adapter = FakeAdapter({_IDENTIFIER: _structure("regulation")})

    _trigger(rig, RigRunner(rig, _NEW_ID), emitter, adapter=adapter)

    assert adapter.metadata_calls == [_IDENTIFIER]
    assert adapter.structure_calls == []


def test_fresh_emits_one_link_entry_and_one_entry_per_completed_stage(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = _world(succession_rig)

    outcome = _trigger(rig, RigRunner(rig, _NEW_ID), emitter, run_id="run-1")
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
    submitted = [line for line in lines if line["action"] == "submit_succession_group"]
    assert [(line["step"], line["graph"], line["outcome"]) for line in submitted] == [
        ("version", "cra_native", "applied"),
        *[("stage_marker", "cra_native", "applied")] * 4,
        ("link", "cra_native", "applied"),
        ("single_tenant_supersede", "policy_system", "applied"),
        ("clear_marker", "cra_native", "applied"),
    ]


# --- failure and retry (AC-BI-007, AC-BI-008) ---


@pytest.mark.parametrize("failing_stage", _ALL)
def test_a_failed_stage_propagates_and_writes_no_succession(
    failing_stage: str, succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)

    with pytest.raises(RuntimeError, match=failing_stage):
        _trigger(rig, RigRunner(rig, _NEW_ID, fail_at=failing_stage), emitter)

    assert rig.edge_properties(rig.native, _PRIOR_ID, _NEW_ID) == []
    assert rig.status_of(rig.native, _PRIOR_ID) == "active"
    completed = _ALL[: _ALL.index(failing_stage)]
    assert rig.marker_stage(rig.native, _NEW_ID) == (completed[-1] if completed else None)
    assert rig.groups("policy_system") == []


def test_a_retry_after_a_derive_failure_reruns_only_derive_and_merge(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    with pytest.raises(RuntimeError):
        _trigger(rig, RigRunner(rig, _NEW_ID, fail_at="derivation"), emitter)
    retry = RigRunner(rig, _NEW_ID)

    outcome = _trigger(rig, retry, emitter)

    assert retry.calls == [("derivation", "merge")]
    assert outcome.state == "resume"
    assert outcome.outcome == "superseded"
    assert rig.edge_properties(rig.native, _PRIOR_ID, _NEW_ID) == [
        {"identity": _EDGE_IDENTITY, "absorbed": True}
    ]
    assert rig.marker_stage(rig.native, _NEW_ID) is None


def test_a_crash_between_merge_and_link_links_without_running_a_stage(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = _world(succession_rig, new_node=True, marker="merge")
    runner = RigRunner(rig, _NEW_ID)

    outcome = _trigger(rig, runner, emitter)
    emitter.flush()

    assert runner.calls == []
    assert outcome.outcome == "superseded"
    assert outcome.run_id is None
    assert outcome.stage_summaries == ()
    assert rig.timeline() == [
        "group:cra_native:SUPERSEDED_BY",
        "group:policy_system:SUPERSEDED_BY",
        "group:cra_native:ReingestProgress",
    ]
    actions = [line["action"] for line in read_lines(log_path)]
    assert actions[0] == "classify_reingestion"
    assert actions[-1] == "link_superseded_by"
    assert set(actions[1:-1]) == {"submit_succession_group"}


def test_resume_without_a_marker_reruns_all_four_stages(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    """A partial ingest leaves the node behind; "node exists" must not mean "ingest finished"."""
    emitter, _ = make_emitter()
    rig = _world(succession_rig, new_node=True)
    runner = RigRunner(rig, _NEW_ID)

    _trigger(rig, runner, emitter)

    assert runner.calls == [_ALL]


def test_stages_to_run_without_a_runner_is_an_inconsistent_call_and_writes_nothing(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)

    with pytest.raises(ChangeMonitorStateError):
        _trigger(rig, None, emitter)

    assert rig.rig.store.groups == []


# --- D2: succession is visible in `policy_system`, in three idempotent steps ---


def test_a_completed_succession_is_written_into_policy_system_too(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)

    _trigger(rig, RigRunner(rig, _NEW_ID), emitter)

    assert rig.status_of(rig.single_tenant, _PRIOR_ID) == "superseded"
    assert rig.status_of(rig.single_tenant, _NEW_ID) == "active"
    assert rig.edge_properties(rig.single_tenant, _PRIOR_ID, _NEW_ID) == [
        {"identity": _EDGE_IDENTITY}
    ]


def test_a_failed_stage_leaves_policy_system_untouched(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)

    with pytest.raises(RuntimeError):
        _trigger(rig, RigRunner(rig, _NEW_ID, fail_at="merge"), emitter)

    assert rig.groups("policy_system") == []
    assert rig.status_of(rig.single_tenant, _PRIOR_ID) == "active"


def test_a_crash_after_the_native_link_finalizes_on_the_next_call(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """Step 1 (native edge + `linked` marker) landed, step 2 (`policy_system`) did not."""
    emitter, log_path = make_emitter()
    rig = _world(succession_rig, linked_absorbed=True, marker="linked", legacy_edge=False)
    runner = RigRunner(rig, _NEW_ID)

    outcome = _trigger(rig, runner, emitter)
    emitter.flush()

    assert runner.calls == []
    assert (outcome.outcome, outcome.state, outcome.run_id) == ("superseded", "finalize", None)
    assert rig.status_of(rig.single_tenant, _PRIOR_ID) == "superseded"
    assert rig.edge_properties(rig.single_tenant, _PRIOR_ID, _NEW_ID) == [
        {"identity": _EDGE_IDENTITY}
    ]
    assert rig.marker_stage(rig.native, _NEW_ID) is None
    assert rig.timeline() == [  # the native link is NOT written again
        "group:policy_system:SUPERSEDED_BY",
        "group:cra_native:ReingestProgress",
    ]
    actions = [line["action"] for line in read_lines(log_path)]
    assert actions[0] == "classify_reingestion"
    assert actions[-1] == "link_superseded_by"


def test_after_finalize_the_next_call_is_already_processed(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig, linked_absorbed=True, marker="linked")
    _trigger(rig, None, emitter)
    logged = len(rig.rig.store.groups)

    again = _trigger(rig, None, emitter)

    assert again.outcome == "already_processed"
    assert len(rig.rig.store.groups) == logged


def test_a_missing_prior_in_policy_system_is_an_inconsistent_graph_and_keeps_the_marker(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig, linked_absorbed=True, marker="linked")
    rig.single_tenant.nodes.clear()

    with pytest.raises(ChangeMonitorStateError, match=_PRIOR_ID):
        _trigger(rig, None, emitter)

    assert rig.marker_stage(rig.native, _NEW_ID) == "linked"  # still finalizable on the next sweep
    assert rig.groups("policy_system") == []


# --- already_processed (AC-BI-009) ---


def test_already_processed_runs_no_stage_writes_nothing_and_logs_no_link(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    rig = _world(succession_rig, linked_absorbed=True)
    runner = RigRunner(rig, _NEW_ID)
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    outcome = _trigger(rig, runner, emitter, adapter=adapter)
    emitter.flush()

    assert outcome.outcome == "already_processed"
    assert outcome.state == "already_processed"
    assert outcome.run_id is None
    assert outcome.stage_summaries == ()
    assert outcome.prior_regulatory_instrument_id == _PRIOR_ID
    assert runner.calls == []
    assert adapter.calls == []
    assert rig.rig.store.groups == []
    assert [line["action"] for line in read_lines(log_path)] == ["classify_reingestion"]


# --- national_transposition guard (AC-010 / AC-BI-005) ---


@pytest.mark.parametrize(
    "world",
    [
        pytest.param({"prior_type": "national_transposition"}, id="fresh"),
        pytest.param(
            {"prior_type": "national_transposition", "new_node": True, "marker": "extraction"},
            id="resume",
        ),
        pytest.param(
            {"prior_type": "national_transposition", "linked_absorbed": None}, id="repair"
        ),
    ],
)
def test_a_national_transposition_prior_is_rejected_before_any_stage_or_write(
    world: dict[str, object], succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig, **world)  # pyright: ignore[reportArgumentType]
    runner = RigRunner(rig, _NEW_ID)
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    with pytest.raises(NationalTranspositionNotSupportedError) as excinfo:
        _trigger(rig, runner, emitter, adapter=adapter)

    assert "#41" in str(excinfo.value)
    assert runner.calls == []
    assert rig.rig.store.groups == []
    assert adapter.calls == []  # limb 1 fires before the metadata fetch; resume/repair never fetch


def test_fetched_national_transposition_metadata_is_rejected_on_fresh(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig)
    runner = RigRunner(rig, _NEW_ID)
    adapter = FakeAdapter({_IDENTIFIER: _structure("national_transposition")})

    with pytest.raises(NationalTranspositionNotSupportedError):
        _trigger(rig, runner, emitter, adapter=adapter)

    assert adapter.metadata_calls == [_IDENTIFIER]
    assert runner.calls == []
    assert rig.rig.store.groups == []


def test_resume_makes_no_adapter_call(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    rig = _world(succession_rig, new_node=True, marker="ingestion")
    adapter = FakeAdapter({_IDENTIFIER: _structure()})

    _trigger(rig, RigRunner(rig, _NEW_ID), emitter, adapter=adapter)

    assert adapter.calls == []


# --- the read-only `will_reingest` probe: "at least one stage will run" ---


@pytest.mark.parametrize(
    ("world", "expected"),
    [
        pytest.param({}, True, id="fresh"),
        pytest.param({"new_node": True, "marker": "extraction"}, True, id="resume-with-stages"),
        pytest.param({"new_node": True, "marker": "merge"}, False, id="link-only"),
        pytest.param({"linked_absorbed": True}, False, id="already_processed"),
        pytest.param({"linked_absorbed": None}, True, id="repair"),
    ],
)
def test_will_reingest_is_true_exactly_when_a_stage_will_run_and_writes_nothing(
    world: dict[str, object], succession_rig: SuccessionRig, *, expected: bool
) -> None:
    rig = _world(succession_rig, **world)  # pyright: ignore[reportArgumentType]

    assert will_reingest(rig.read_native(), "CRA", "2.0") is expected
    assert rig.rig.store.groups == []


def test_will_reingest_raises_when_the_graph_has_no_single_active_prior(
    succession_rig: SuccessionRig,
) -> None:
    with pytest.raises(ChangeMonitorStateError):
        will_reingest(succession_rig.read_native(), "CRA", "2.0")


def test_finalizing_a_pre_gateway_link_stamps_its_edge_in_the_link_group(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    """A `finalize` after an upgrade: the native edge has no identity, so the link group stamps it
    (no parallel edge) before the `policy_system` and marker steps complete.
    """
    emitter, _ = make_emitter()
    rig = _world(succession_rig, linked_absorbed=True, marker="linked")  # legacy edge

    _trigger(rig, None, emitter)

    assert rig.edge_properties(rig.native, _PRIOR_ID, _NEW_ID) == [
        {"identity": _EDGE_IDENTITY, "absorbed": True}
    ]
    assert rig.timeline() == [
        "group:cra_native:SUPERSEDED_BY",
        "group:policy_system:SUPERSEDED_BY",
        "group:cra_native:ReingestProgress",
    ]
