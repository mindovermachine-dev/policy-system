"""`trigger_reingestion` -- the UC-4 re-entry point (AC-004..AC-009).

Given a base-act `identifier`, its `short_name`, and the `new_version` to record, run the
full re-ingestion cycle for the new version -- Ingestion -> Domain Mapper -> Company Merge --
and only then record the `SUPERSEDED_BY` succession from the prior active version to the new
one. Succession is the LAST write: an amendment is never marked superseded while its
Obligations and Capabilities are unmapped.

The stages are not called from here. `change_monitor` must not import `ps_service.api`, so the
shared stage sequence (the catalog pipeline's `_execute_catalog_stages`) is handed in as an
injected `PipelineRunner`; this module decides WHICH stages still need to run, from durable
graph facts, and writes the bookkeeping around them.

Decomposition: `classify_reingestion` is a pure, read-only classification of the
`{short_name}_native` graph (see its docstring for the states and the stages each one still
needs); `trigger_reingestion`'s body is a flat orchestration over that result with no nested
conditionals (complexity <= 8).

Ordering: classify -> (`already_processed`: return) -> `national_transposition` guard (before
any stage or write) -> run the missing stages, writing a `ReingestProgress` marker after each
one returns (and `new.version` right after ingestion) -> the native fused `link_and_supersede` ->
the `policy_system` write -> clear the marker -> emit one `link_superseded_by` entry. No
try/except around the runner: a stage failure propagates unchanged, the completed-stage markers
stay, no link is written and the prior stays `active`, so the next call resumes at the first
missing stage. Concurrent sweeps are not atomic; one sweep per caller is the accepted model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    NationalTranspositionNotSupportedError,
)
from ps_service.change_monitor.models import LINKED, PIPELINE_STAGES, ReingestionOutcome
from ps_service.change_monitor.succession import (
    clear_marker,
    find_prior_instrument,
    link_and_supersede,
    mark_stage_complete,
    read_reingestion_facts,
    set_new_version_property,
    supersede_in_single_tenant,
)
from ps_service.ingestion.adapters.base import IngestionAdapter
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from ps_service.change_monitor.falkordb_client import GraphHandle
    from ps_service.change_monitor.models import PipelineRunner
    from ps_service.change_monitor.succession import ReingestionFacts
    from ps_service.ingestion.models import RegulatoryInstrumentMetadata
    from ps_service.logging import LogEmitter

_COMPONENT = "change_monitor"
_LINK_ACTION = "link_superseded_by"
_LINK_OUTCOME = "superseded"
_CLASSIFY_ACTION = "classify_reingestion"
_STAGE_ACTION = "run_pipeline_stage"
_STAGE_OUTCOME = "succeeded"

# The one instrument type `trigger_reingestion` refuses (AC-010). It is an
# instrument-type token, not a regulation name or CELEX, so it is free of the
# AC-011 "no regulation literal in a conditional" constraint.
_NATIONAL_TRANSPOSITION = "national_transposition"


class MetadataFetchingAdapter(IngestionAdapter, Protocol):
    """An `IngestionAdapter` that can also fetch an instrument's metadata alone.

    Scoped to `change_monitor` rather than added to the shared
    `IngestionAdapter`, which maps 1:1 to the CA doc's single
    `FetchRegulatoryInstrumentStructure` action: the guard only needs
    `instrument_type`, and no other caller needs the capability.
    """

    def fetch_regulatory_instrument_metadata(self, identifier: str) -> RegulatoryInstrumentMetadata:
        """Fetch `identifier`'s metadata alone, without parsing its structure."""
        ...


@dataclass(frozen=True, slots=True)
class _Preflight:
    """The read-only classification of the graph state before any write.

    `prior_id` is the prior instrument's id: the single active prior for `fresh` /
    `resume`, the edge's prior for `repair` / `finalize` / `already_processed`.
    `prior_instrument_type` feeds the AC-010 guard. `stages_to_run` are the pipeline
    stages still missing for the new version (empty when only the link is outstanding).
    """

    state: Literal["fresh", "resume", "repair", "already_processed", "finalize"]
    prior_id: str | None
    prior_instrument_type: str | None
    stages_to_run: tuple[str, ...] = ()


def _stages_after(marker_stage: str | None) -> tuple[str, ...]:
    """The pipeline stages still to run after `marker_stage` completed (none marked: all four).

    `linked` and `merge` both leave nothing to run. An unknown marker value is an
    inconsistent graph, never silently treated as "start over".
    """
    if marker_stage is None:
        return PIPELINE_STAGES
    if marker_stage == LINKED:
        return ()
    if marker_stage not in PIPELINE_STAGES:
        raise ChangeMonitorStateError(f"unknown ReingestProgress stage {marker_stage!r}")
    return PIPELINE_STAGES[PIPELINE_STAGES.index(marker_stage) + 1 :]


def classify_reingestion(
    graph: GraphHandle, new_id: str, *, emitter: LogEmitter | None = None
) -> _Preflight:
    """Classify the graph state for `new_id` from durable facts only (read-only).

    One facts query (node / incoming `SUPERSEDED_BY` edge / `ReingestProgress`
    marker) plus `find_prior_instrument` only when no edge names the prior. States:

    - `fresh`: the new node is absent -> all four stages.
    - `resume`: node present, no edge -> the stages after the marker's stage (all
      four with no marker: a partial ingest also leaves the node behind).
    - `already_processed`: edge with `absorbed=true`, no marker -> nothing to do.
    - `finalize`: edge with `absorbed=true` and a `linked` marker -> only the
      `policy_system` write and marker delete are outstanding (stages none).
    - `repair`: edge with `absorbed` unset, an earlier ingestion-only link -> the
      stages after the marker's (extraction onward with no marker).

    Raises `ChangeMonitorStateError` for an edge whose prior is not `superseded`,
    an unknown marker value, or (fresh / resume) no single active prior.
    """
    pre = _classify(graph, new_id)
    emit_log_entry(
        component=_COMPONENT,
        action=_CLASSIFY_ACTION,
        entity_id=new_id,
        outcome=pre.state,
        extra={"stages_to_run": list(pre.stages_to_run), "prior_id": pre.prior_id},
        emitter=emitter,
    )
    return pre


def _classify(graph: GraphHandle, new_id: str) -> _Preflight:
    """The read-only classification behind :func:`classify_reingestion`, without the log entry."""
    facts = read_reingestion_facts(graph, new_id)
    if facts.prior_id is not None:
        return _classify_edge(facts)
    stages = _stages_after(facts.marker_stage) if facts.node_exists else PIPELINE_STAGES
    prior = find_prior_instrument(graph, new_id)
    return _Preflight(
        state="resume" if facts.node_exists else "fresh",
        prior_id=prior.id,
        prior_instrument_type=prior.instrument_type,
        stages_to_run=stages,
    )


def _classify_edge(facts: ReingestionFacts) -> _Preflight:
    """Classify a new version that already has a `SUPERSEDED_BY` edge into it."""
    if facts.prior_status != "superseded":
        raise ChangeMonitorStateError(
            f"{facts.prior_id!r} has a SUPERSEDED_BY edge but status {facts.prior_status!r}"
        )
    if facts.absorbed:
        state = "finalize" if facts.marker_stage == LINKED else "already_processed"
        return _Preflight(state, facts.prior_id, facts.prior_instrument_type)
    # Legacy ingestion-only link: ingest already finished (the old code wrote the edge
    # only after it), so a missing marker starts at extraction, not at ingestion.
    stages = _stages_after(facts.marker_stage or PIPELINE_STAGES[0])
    return _Preflight("repair", facts.prior_id, facts.prior_instrument_type, stages)


def _guard_national_transposition(
    preflight: _Preflight, *, adapter: MetadataFetchingAdapter, identifier: str
) -> None:
    """Reject a `national_transposition` instrument before any stage or write (AC-010, AC-BI-005).

    Two limbs (PLAN_REVIEWED.md §1.4, flaws 10 + 11):

    1. The prior node's `instrument_type`, carried through the classification. Checked for
       EVERY state that still has stages to run (`fresh`, `resume`, `repair`), so Domain Mapper
       and Company Merge can never write for a `national_transposition` prior. This is the
       *only* limb that can actually fire for `CellarEliAdapter` today.
    2. Forward-looking defence, `fresh` only: one `fetch_regulatory_instrument_metadata` call
       (metadata only, no structural parse), rejecting the fetched metadata's
       `instrument_type`. *Untested-by-construction for `CellarEliAdapter`* -- its type-code
       map is `{R: regulation, L: directive}` and it raises `CellarParseError` for anything
       else. A resumable state implies the original ingest, gated by this guard, already
       passed it, which keeps `resume` / `repair` free of HTTP.

    Only the `== national_transposition` comparison is made here -- there is deliberately no
    `regulation` vs `directive` branch (AC-010/AC-011): the two framework types take the
    identical path.
    """
    if preflight.prior_instrument_type == _NATIONAL_TRANSPOSITION:
        raise NationalTranspositionNotSupportedError
    if preflight.state != "fresh":
        return
    metadata = adapter.fetch_regulatory_instrument_metadata(identifier)
    if metadata.instrument_type == _NATIONAL_TRANSPOSITION:
        raise NationalTranspositionNotSupportedError


def will_reingest(graph: GraphHandle, short_name: str, new_version: str) -> bool:
    """Whether `trigger_reingestion` would run at least one pipeline stage (read-only probe, #195).

    True for `fresh`, a `resume` or `repair` with stages left, and false for `already_processed`
    and a link-only call (nothing to run, so the sweep writes no audit pair for it). Raises the
    same `ChangeMonitorStateError` as `trigger_reingestion` when the graph is inconsistent. The
    probe and the later call are not atomic; a single sweep per caller is the accepted model.
    """
    return bool(_classify(graph, f"{short_name}-{new_version}").stages_to_run)


def trigger_reingestion(  # noqa: PLR0913 -- one re-ingest's collaborators; no natural grouping
    identifier: str,
    short_name: str,
    new_version: str,
    *,
    adapter: MetadataFetchingAdapter,
    graph: GraphHandle,
    single_tenant: GraphHandle,
    emitter: LogEmitter | None = None,
    run_id: str | None = None,
    run_pipeline: PipelineRunner | None = None,
) -> ReingestionOutcome:
    """Re-ingest `identifier` as `new_version` through the full pipeline and record its succession.

    `graph` is the `{short_name}_native` handle: the `ReingestProgress` markers, `new.version`
    and the native succession write land there. `single_tenant` is the merged `policy_system`
    handle: the succession is mirrored there so the tracked set and `ps-list-ingested` see the
    prior as `superseded`. `identifier` is whatever the injected `adapter`
    expects (a base-act CELEX for `CellarEliAdapter`). `run_pipeline` runs a subset of the
    stages (Ingestion, Domain Mapper extract, derive, Company Merge) for the new version; it is
    needed only when stages are outstanding and its absence then raises
    `ChangeMonitorStateError` before any write.

    States (see :func:`classify_reingestion`): `fresh` runs all four stages; `resume` and
    `repair` run only the stages after the last `ReingestProgress` marker (a `repair` is an
    earlier ingestion-only link whose new version was never mapped); with every stage done
    only the link is (re)written; `already_processed` is a no-op that emits nothing.

    The succession is written last, after the merge stage returned, in three idempotent steps:
    the native fused write (`SUPERSEDED_BY` edge + `absorbed` + `prior.status='superseded'` +
    marker `linked`), the `policy_system` write (edge + status), then the marker is cleared. A
    crash between steps leaves the `linked` marker, which classifies as `finalize` and resumes
    at step 2 on the next call. Then one
    `link_superseded_by` entry is emitted carrying the pipeline's `run_id`. `run_id` (issue
    #195) lets a caller that audits the run know its id up front; `None` mints one. The
    returned `run_id` is `None` when no stage ran.

    Raises `NationalTranspositionNotSupportedError` (AC-010, before any write),
    `ChangeMonitorStateError` (inconsistent graph, or no runner for outstanding stages), or
    whatever the runner raises (a stage failure; nothing is written for the link).
    """
    new_id = f"{short_name}-{new_version}"
    pre = classify_reingestion(graph, new_id, emitter=emitter)
    prior_id = pre.prior_id
    if prior_id is None:  # unreachable: every classification carries a prior id
        raise ChangeMonitorStateError(f"classification of {new_id!r} carried no prior id")

    if pre.state == "already_processed":
        return ReingestionOutcome(prior_id, new_id, None, "already_processed", pre.state)

    stage_summaries = ()
    pipeline_run_id: str | None = None
    if pre.stages_to_run:
        _guard_national_transposition(pre, adapter=adapter, identifier=identifier)
        if run_pipeline is None:
            raise ChangeMonitorStateError(
                f"{new_id!r} has stages to run {pre.stages_to_run} but no pipeline runner was given"
            )
        pipeline_run_id = run_id or str(uuid.uuid4())
        stage_summaries = run_pipeline(
            stages=pre.stages_to_run,
            run_id=pipeline_run_id,
            on_stage_complete=lambda stage: _record_stage(
                graph, new_id, new_version, stage, emitter
            ),
        ).stages

    link_and_supersede(graph, prior_id, new_id)
    supersede_in_single_tenant(single_tenant, prior_id, new_id)
    clear_marker(graph, new_id)
    _emit_link(prior_id, new_id, run_id=pipeline_run_id, emitter=emitter)
    return ReingestionOutcome(
        prior_id, new_id, pipeline_run_id, "superseded", pre.state, stage_summaries
    )


def _record_stage(
    graph: GraphHandle, new_id: str, new_version: str, stage: str, emitter: LogEmitter | None
) -> None:
    """Persist that `stage` returned: the version after ingestion, then the progress marker.

    Runs only after the stage returned, so a marker is a durable "this stage is done" fact in
    the store the succession lives in. `new.version` is written right after ingestion, before
    extract copies the node's properties into the baseline graph.
    """
    if stage == PIPELINE_STAGES[0]:
        set_new_version_property(graph, new_id, new_version)
    mark_stage_complete(graph, new_id, stage)
    emit_log_entry(
        component=_COMPONENT,
        action=_STAGE_ACTION,
        entity_id=new_id,
        outcome=_STAGE_OUTCOME,
        extra={"stage": stage},
        emitter=emitter,
    )


def _emit_link(
    prior_id: str, new_id: str, *, run_id: str | None, emitter: LogEmitter | None
) -> None:
    """Emit the single `link_superseded_by` entry after the succession write succeeds.

    `entity_id` is the `(prior_id, new_id)` tuple -- `LogEntry.to_json_line`
    serialises it as a 2-element JSON array, the carrier for AC-009's "old +
    new regulatory_instrument_id on one entry". `run_id` is passed explicitly
    (the pipeline's own run context has already exited) and is `None` when
    only the link was written.
    """
    emit_log_entry(
        component=_COMPONENT,
        action=_LINK_ACTION,
        entity_id=(prior_id, new_id),
        outcome=_LINK_OUTCOME,
        run_id=run_id,
        emitter=emitter,
    )
