"""`trigger_reingestion` -- the re-entry point that re-ingests an amended instrument.

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
needs); `trigger_reingestion`'s body is a flat orchestration over that result.

Ordering: classify -> (`already_processed`: return) -> `national_transposition` guard (before
any stage or write) -> run the missing stages, writing a `ReingestProgress` marker after each
one returns (and `new.version` right after ingestion) -> the native fused `link_and_supersede` ->
the `policy_system` write -> clear the marker -> emit one `link_superseded_by` entry. No
try/except around the runner: a stage failure propagates unchanged, the completed-stage markers
stay, no link is written and the prior stays `active`, so the next call resumes at the first
missing stage. Concurrent sweeps are not atomic; the design assumes one sweep per caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    NationalTranspositionNotSupportedError,
)
from ps_service.change_monitor.falkordb_client import native_graph_name, single_tenant_graph_name
from ps_service.change_monitor.graph_submission import SuccessionTarget
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
    from collections.abc import Callable

    from ps_service.change_monitor.falkordb_client import GraphHandle
    from ps_service.change_monitor.models import PipelineRunner
    from ps_service.change_monitor.succession import ReingestionFacts
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.ingestion.models import RegulatoryInstrumentMetadata
    from ps_service.logging import LogEmitter

_COMPONENT = "change_monitor"
_LINK_ACTION = "link_superseded_by"
_LINK_OUTCOME = "superseded"
_CLASSIFY_ACTION = "classify_reingestion"
_STAGE_ACTION = "run_pipeline_stage"
_STAGE_OUTCOME = "succeeded"

# The one instrument type `trigger_reingestion` refuses. It is an instrument-type token, not a
# regulation name or CELEX, so comparing against it does not special-case any regulation.
_NATIONAL_TRANSPOSITION = "national_transposition"


class MetadataFetchingAdapter(IngestionAdapter, Protocol):
    """An `IngestionAdapter` that can also fetch an instrument's metadata alone.

    Scoped to `change_monitor` rather than added to the shared `IngestionAdapter`, whose single
    action is fetching a full instrument structure: the national-transposition guard only needs
    `instrument_type`, and no other caller needs the capability.
    """

    def fetch_regulatory_instrument_metadata(self, identifier: str) -> RegulatoryInstrumentMetadata:
        """Fetch `identifier`'s metadata alone, without parsing its structure.

        Args:
            identifier: Instrument identifier in the form the adapter expects (a base-act
                CELEX for `CellarEliAdapter`).

        Returns:
            The instrument's `RegulatoryInstrumentMetadata`, including `instrument_type`.
        """
        ...


@dataclass(frozen=True, slots=True)
class _Preflight:
    """The read-only classification of the graph state before any write.

    Attributes:
        state: Classification the re-ingest acts on.
        prior_id: The prior instrument's id: the single active prior for `fresh` / `resume`,
            the edge's prior for `repair` / `finalize` / `already_processed`.
        prior_instrument_type: The prior's instrument type; feeds the national-transposition
            guard.
        stages_to_run: Pipeline stages still missing for the new version; empty when only the
            link is outstanding.
    """

    state: Literal["fresh", "resume", "repair", "already_processed", "finalize"]
    prior_id: str | None
    prior_instrument_type: str | None
    stages_to_run: tuple[str, ...] = ()


def _stages_after(marker_stage: str | None) -> tuple[str, ...]:
    """Return the pipeline stages still to run after `marker_stage` completed.

    `None` (no marker) yields all four stages; `linked` and `merge` both leave nothing to run.
    An unknown marker value is an inconsistent graph and raises `ChangeMonitorStateError`,
    never silently treated as "start over".
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

    Decides which pipeline stages a re-ingest still needs, so a failed run resumes where it
    stopped. One facts query (node / incoming `SUPERSEDED_BY` edge / `ReingestProgress`
    marker) plus `find_prior_instrument` only when no edge names the prior. States:

    - `fresh`: the new node is absent -> all four stages.
    - `resume`: node present, no edge -> the stages after the marker's stage (all
      four with no marker: a partial ingest also leaves the node behind).
    - `already_processed`: edge with `absorbed=true`, no marker -> nothing to do.
    - `finalize`: edge with `absorbed=true` and a `linked` marker -> only the
      `policy_system` write and marker delete are outstanding (stages none).
    - `repair`: edge with `absorbed` unset, an earlier ingestion-only link -> the
      stages after the marker's (extraction onward with no marker).

    Args:
        graph: Read handle onto the `{short_name}_native` graph.
        new_id: Graph id of the new version, `{short_name}-{new_version}`.
        emitter: Log emitter for the classification entry; `None` uses the default emitter.

    Returns:
        The classification: state, prior id and type, and the stages still to run.

    Raises:
        ChangeMonitorStateError: an edge's prior is not `superseded`, a marker value is
            unknown, or (fresh / resume) there is no single active prior.
        SuccessionPersistenceError: FalkorDB could not be queried.

    Side Effects:
        Read-only queries on `graph`; emits one `classify_reingestion` log entry.
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
    # An ingestion-only link (`absorbed` unset) was written only after ingest finished, so a
    # missing marker starts at extraction, not at ingestion.
    stages = _stages_after(facts.marker_stage or PIPELINE_STAGES[0])
    return _Preflight("repair", facts.prior_id, facts.prior_instrument_type, stages)


def _guard_national_transposition(
    preflight: _Preflight, *, adapter: MetadataFetchingAdapter, identifier: str
) -> None:
    """Reject a `national_transposition` instrument before any stage or write.

    Two checks:

    1. The prior node's `instrument_type`, carried through the classification. Checked for
       EVERY state that still has stages to run (`fresh`, `resume`, `repair`), so Domain Mapper
       and Company Merge can never write for a `national_transposition` prior. This is the
       *only* check that can fire for `CellarEliAdapter`.
    2. Defence for adapters that can return the type, `fresh` only: one
       `fetch_regulatory_instrument_metadata` call (metadata only, no structural parse),
       rejecting the fetched metadata's `instrument_type`. `CellarEliAdapter` maps only
       `{R: regulation, L: directive}` and raises `CellarParseError` for any other type code,
       so this check cannot fire for it. A resumable state implies the original ingest already
       passed this guard, which keeps `resume` / `repair` free of HTTP.

    Only the `== national_transposition` comparison is made here; there is deliberately no
    `regulation` vs `directive` branch: the two framework types take the identical path.

    Args:
        preflight: The classification from :func:`classify_reingestion`.
        adapter: Adapter used for the metadata fetch in the `fresh` state.
        identifier: Instrument identifier in the form `adapter` expects.

    Raises:
        NationalTranspositionNotSupportedError: the prior or the fetched instrument is a
            `national_transposition`; nothing has been written.

    Side Effects:
        In the `fresh` state, one metadata fetch through `adapter` (an external call).
    """
    if preflight.prior_instrument_type == _NATIONAL_TRANSPOSITION:
        raise NationalTranspositionNotSupportedError
    if preflight.state != "fresh":
        return
    metadata = adapter.fetch_regulatory_instrument_metadata(identifier)
    if metadata.instrument_type == _NATIONAL_TRANSPOSITION:
        raise NationalTranspositionNotSupportedError


def will_reingest(graph: GraphHandle, short_name: str, new_version: str) -> bool:
    """Return whether `trigger_reingestion` would run at least one pipeline stage (read-only).

    Lets a caller that audits runs decide up front whether to record one: a link-only or
    `already_processed` call runs nothing, so the sweep writes no audit pair for it. The probe
    and the later call are not atomic; the design assumes one sweep per caller.

    Args:
        graph: Read handle onto the `{short_name}_native` graph.
        short_name: Instrument short name, e.g. `gdpr`.
        new_version: Version string of the new version; with `short_name` forms the new id.

    Returns:
        True for `fresh`, and for a `resume` or `repair` with stages left; false for
        `already_processed` and a link-only call.

    Raises:
        ChangeMonitorStateError: the graph is inconsistent, as for `trigger_reingestion`.
        SuccessionPersistenceError: FalkorDB could not be queried.

    Side Effects:
        Read-only queries on `graph`; no log entry is emitted.
    """
    return bool(_classify(graph, f"{short_name}-{new_version}").stages_to_run)


def trigger_reingestion(  # noqa: PLR0913 -- one re-ingest's collaborators; no natural grouping
    identifier: str,
    short_name: str,
    new_version: str,
    *,
    adapter: MetadataFetchingAdapter,
    graph: GraphHandle,
    submitter: GroupSubmitter,
    audit_anchor: Callable[[], str],
    emitter: LogEmitter | None = None,
    run_id: str | None = None,
    run_pipeline: PipelineRunner | None = None,
) -> ReingestionOutcome:
    """Re-ingest `identifier` as `new_version` through the full pipeline and record its succession.

    Every write goes through `submitter`, the Graph Write Gateway: the `ReingestProgress`
    markers, `new.version` and the native succession land on the `{short_name}_native` graph,
    and the succession is mirrored on the merged `policy_system` graph so the tracked set and
    `ps-list-ingested` see the prior as `superseded`. Each group carries the audit event id
    `audit_anchor()` returns (the re-ingest's `ingestion_run.submit` row).

    States (see :func:`classify_reingestion`): `fresh` runs all four stages; `resume` and
    `repair` run only the stages after the last `ReingestProgress` marker (a `repair` is an
    earlier ingestion-only link whose new version was never mapped); with every stage done
    only the link is (re)written; `already_processed` is a no-op that emits nothing.

    The succession is written last, after the merge stage returned, in three idempotent steps:
    the native fused write (`SUPERSEDED_BY` edge + `absorbed` + `prior.status='superseded'` +
    marker `linked`), the `policy_system` write (edge + status), then the marker is cleared. A
    crash between steps leaves the `linked` marker, which classifies as `finalize` and resumes
    at step 2 on the next call. Each step is one gateway group; a group committed but not yet
    applied raises `SuccessionPersistenceError`. Then one `link_superseded_by` entry is emitted
    carrying the pipeline's `run_id`.

    Args:
        identifier: Instrument identifier in the form the injected `adapter` expects (a
            base-act CELEX for `CellarEliAdapter`).
        short_name: Instrument short name; names the `{short_name}_native` graph.
        new_version: Version string to record; with `short_name` forms the new id
            `{short_name}-{new_version}`.
        adapter: Adapter used only for the national-transposition metadata check.
        graph: Read handle onto the `{short_name}_native` graph, used for classification.
        submitter: Graph Write Gateway submitter through which every write goes.
        audit_anchor: Callable returning the audit event id each write group is anchored to;
            not called when nothing is written.
        emitter: Log emitter for the entries; `None` uses the default emitter.
        run_id: Pipeline run id, so a caller that audits the run knows it up front; `None`
            mints one.
        run_pipeline: Runs a subset of the stages (Ingestion, Domain Mapper extract, derive,
            Company Merge). Needed only when stages are outstanding.

    Returns:
        A `ReingestionOutcome`: `superseded` when this call completed the succession,
        `already_processed` when it was already complete. Its `run_id` is `None` when no stage
        ran.

    Raises:
        NationalTranspositionNotSupportedError: the prior or new instrument is a
            `national_transposition`; raised before any write.
        ChangeMonitorStateError: the graph is inconsistent, or stages are outstanding and
            `run_pipeline` is `None`; raised before any write.
        SuccessionPersistenceError: a gateway group was refused, was committed but not applied,
            or the stores were unreachable.
        Exception: whatever `run_pipeline` raises for a stage failure; no link is written and
            the prior stays active.

    Side Effects:
        Runs the outstanding pipeline stages, writes markers, the version and the succession
        through the gateway, and emits `classify_reingestion`, `run_pipeline_stage` and
        `link_superseded_by` log entries.
    """
    new_id = f"{short_name}-{new_version}"
    target = SuccessionTarget(
        submitter,
        native_graph_name(short_name),
        single_tenant_graph_name(),
        audit_anchor,
        emitter,
    )
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
                target, new_id, new_version, stage, emitter
            ),
        ).stages

    link_and_supersede(target, prior_id, new_id)
    supersede_in_single_tenant(target, prior_id, new_id)
    clear_marker(target, new_id)
    _emit_link(prior_id, new_id, run_id=pipeline_run_id, emitter=emitter)
    return ReingestionOutcome(
        prior_id, new_id, pipeline_run_id, "superseded", pre.state, stage_summaries
    )


def _record_stage(
    target: SuccessionTarget,
    new_id: str,
    new_version: str,
    stage: str,
    emitter: LogEmitter | None,
) -> None:
    """Persist that `stage` returned: the version after ingestion, then the progress marker.

    Runs only after the stage returned, so a marker is a durable "this stage is done" fact in
    the store the succession lives in. `new.version` is written right after ingestion, before
    extract copies the node's properties into the baseline graph.
    """
    if stage == PIPELINE_STAGES[0]:
        set_new_version_property(target, new_id, new_version)
    mark_stage_complete(target, new_id, stage)
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
    serialises it as a 2-element JSON array, carrying the old and new
    `regulatory_instrument_id` on one entry. `run_id` is passed explicitly
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
