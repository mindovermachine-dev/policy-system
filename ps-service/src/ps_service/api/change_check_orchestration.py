"""REST-boundary glue for `POST /change-checks`, the amendment sweep over tracked instruments.

The sweep opens the merged `policy_system` graph once, reads the tracked-instrument set from
it once, polls every tracked instrument for a newer consolidated version, and classifies each
instrument as `current`, `poll_failed`, `not_configured`, `amendment_reingested`, `skipped` or
`reingest_failed`. A failure of one instrument is reported for that instrument and the sweep
continues with the next.

This module provides the injection seam :class:`ChangeCheckDependencies`, the production wiring
:func:`build_default_change_check_dependencies`, and the entry point
:func:`run_change_check_sweep` that the route calls. The production wiring imports
`ps_service.change_monitor` function-locally, so importing `ps_service.main` never loads
Regulatory Change Monitor at module load.

A detected amendment runs the same stage sequence the catalog pipeline uses (Ingestion, Domain
Mapper extract and derive, Company Merge) through a `PipelineRunner` built here and injected
into `trigger_reingestion`, because `change_monitor` must not import `ps_service.api`. The
runner checks the pipeline configuration first (a missing chat model, embed model or merge
threshold is a `reingest_failed` before any graph write); the succession is written only after
the merge stage returned, so a failed stage leaves the prior version `active` and the next sweep
re-runs only the missing stages.

A national-transposition refusal (an exception whose class is named
`NationalTranspositionNotSupportedError`, matched by name and never imported) becomes
`skipped`; any other exception becomes `reingest_failed`. Neither propagates past the
per-instrument boundary. A `reingest_failed` instrument's `detail` is never exception text: it
is the enumerated audit `reason_code`, plus the failing pipeline stage when one is known
(`pipeline_stage_failed (stage: derivation)`). The full failure detail is logged server-side
only.

The sweep mints no run id of its own. It reuses the `run_id` the route bound for the request,
and every log entry this module emits carries it through an explicit `run_id=` argument, never
through `contextvars` inheritance, because `poll_for_amendments` and `trigger_reingestion` may
bind their own nested run ids.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from ps_service.api.catalog import CatalogEntry, find_by_celex
from ps_service.api.errors import PipelineStageError
from ps_service.api.graph_write_access import GraphWriteAccess, graph_write_access
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    _default_ingestion_adapter,  # pyright: ignore[reportPrivateUsage]  -- shared graph/adapter factory reused by the sweep
    _execute_catalog_stages,  # pyright: ignore[reportPrivateUsage]  -- the shared stage sequence the sweep re-uses
    _open_native_graph,  # pyright: ignore[reportPrivateUsage]  -- shared graph opener reused by the sweep
    _open_single_tenant_graph,  # pyright: ignore[reportPrivateUsage]  -- shared graph opener reused by the sweep
    _OpenGraphs,  # pyright: ignore[reportPrivateUsage]  -- the graph bundle the shared stage sequence takes
    _require_ingestion_config,  # pyright: ignore[reportPrivateUsage]  -- config-completeness guard, run first by the runner
    build_default_pipeline_dependencies,
    classify_ingestion_failure,
)
from ps_service.api.run_status import clear_stage
from ps_service.audit.emit import (
    AuditTarget,
    record_follow_up_row,
    record_opening_row_with_event_id,
)
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.ingestion_runs.audit_actions import (
    INGESTION_RUN_COMPLETE_ACTION,
    INGESTION_RUN_RESOURCE_TYPE,
    INGESTION_RUN_SUBMIT_ACTION,
    IngestionReasonCode,
    completion_audit_entry,
    submission_audit_entry,
)
from ps_service.logging.facade import emit_log_entry
from ps_service.logging.run_context import bind_run_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ps_service.api.ingestion_orchestration import GraphHandle
    from ps_service.audit.emit import AuditContext
    from ps_service.change_monitor.models import (
        AmendmentFinding,
        PipelineRunner,
        PollReport,
        ReingestionOutcome,
        TrackedInstrumentNode,
    )
    from ps_service.change_monitor.trigger import MetadataFetchingAdapter
    from ps_service.config import ServiceConfig
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

# Log component and action names, in the shape ingestion_orchestration uses.
_COMPONENT = "api"
_SWEEP_ACTION = "change_check_sweep"
_INSTRUMENT_ACTION = "change_check_instrument"

# Matched by name (not import) so this module never imports
# `ps_service.change_monitor.errors` at load time -- mirrors
# `error_handlers._SAFE_VERBATIM_NAMES`'s own established idiom for the
# identical cross-component situation.
_NATIONAL_TRANSPOSITION_ERROR_NAME = "NationalTranspositionNotSupportedError"

# The six outcome buckets a tracked instrument can land in. Declared here, not re-derived from
# `api.models.InstrumentCheckOutcomeBody`, because this module's dataclasses are internal
# plumbing, kept separate from their `api.models` Pydantic counterparts (as in
# `restore_orchestration`).
InstrumentCheckOutcomeValue = Literal[
    "current",
    "amendment_reingested",
    "poll_failed",
    "not_configured",
    "skipped",
    "reingest_failed",
]


@dataclass(frozen=True, slots=True)
class InstrumentCheckOutcome:
    """One tracked instrument's classification from a change-check sweep.

    Attributes:
        instrument_id: The tracked instrument's `regulatory_instrument_id`.
        outcome: The bucket the instrument landed in.
        detail: Caller-safe explanation, or `None`. For `amendment_reingested` the new instrument id
            and the re-ingest state; for `reingest_failed` the enumerated `reason_code` plus the
            failing stage when known; for `skipped` the domain-level refusal message.
        reingest_run_id: The pipeline run id of the re-ingest, or `None` when no stage ran or no
            re-ingest was attempted.
        reason_code: The enumerated audit reason code of a `skipped` or `reingest_failed` outcome,
            else `None`.
        failing_stage: The pipeline stage that failed, or `None` when not a stage failure.
    """

    instrument_id: str
    outcome: InstrumentCheckOutcomeValue
    detail: str | None = None
    reingest_run_id: str | None = None
    reason_code: IngestionReasonCode | None = None
    failing_stage: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeCheckResult:
    """Result of one change-check sweep.

    Attributes:
        run_id: The request-scoped run id the sweep was given and logged under.
        instruments: One outcome per tracked instrument, in the order the graph returned them;
            empty when no instrument is tracked.
    """

    run_id: str
    instruments: tuple[InstrumentCheckOutcome, ...]


# --- injection seam (mirrors PipelineDependencies and RestoreDependencies) ---


class ReadTrackedInstrumentsCall(Protocol):
    """Call shape of `change_monitor.graph_reader.read_tracked_instruments`."""

    def __call__(self, graph: GraphHandle) -> tuple[TrackedInstrumentNode, ...]:
        """Enumerate every active, external `regulation` or `directive` instrument in `graph`.

        Args:
            graph: Handle onto the merged `policy_system` graph.

        Returns:
            One `TrackedInstrumentNode` per matching instrument, in the graph's result
            order; an empty tuple when none is tracked.

        Raises:
            redis.exceptions.RedisError: The graph query fails because FalkorDB is unreachable.

        Side Effects:
            Runs one read-only query; writes nothing.
        """
        ...


class PollForAmendmentsCall(Protocol):
    """Call shape of `change_monitor.poll.poll_for_amendments`."""

    def __call__(self, graph: GraphHandle, *, emitter: LogEmitter | None = None) -> PollReport:
        """Poll every tracked instrument in `graph` for a newer consolidated version.

        Args:
            graph: Handle onto the merged `policy_system` graph.
            emitter: Log emitter for the per-instrument entries; `None` uses the process default.

        Returns:
            A `PollReport` whose `findings` hold the instruments with a detected amendment, and
            whose `failed_ids` and `unconfigured_ids` hold those whose lookup failed or that have
            no CELEX.

        Raises:
            redis.exceptions.RedisError: The graph read fails. A per-instrument lookup failure
                does not raise; it is reported in `failed_ids`.

        Side Effects:
            Calls the external consolidated-version lookup once per tracked instrument and emits one
            log entry per instrument; writes nothing to the graph.
        """
        ...


class TriggerReingestionCall(Protocol):
    """Call shape of `change_monitor.trigger.trigger_reingestion`."""

    def __call__(  # noqa: PLR0913 -- mirrors trigger_reingestion's signature exactly
        self,
        identifier: str,
        short_name: str,
        new_version: str,
        *,
        adapter: MetadataFetchingAdapter,
        graph: GraphHandle,
        submitter: GroupSubmitter,
        single_tenant_graph_name: str,
        audit_anchor: Callable[[], str],
        emitter: LogEmitter | None = None,
        run_id: str | None = None,
        run_pipeline: PipelineRunner | None = None,
    ) -> ReingestionOutcome:
        """Re-ingest `identifier` as `new_version` through the pipeline and record the succession.

        Args:
            identifier: Instrument identifier in the form `adapter` expects (the base-act CELEX).
            short_name: Instrument short name; names the `{short_name}_native` graph.
            new_version: Version string to record; with `short_name` forms the new instrument id.
            adapter: Adapter used for the national-transposition metadata check.
            graph: Read handle onto the `{short_name}_native` graph, used to classify the re-ingest.
            submitter: Graph Write Gateway submitter through which every write goes.
            single_tenant_graph_name: Name of the merged single-tenant graph the succession is
                mirrored on.
            audit_anchor: Returns the audit event id each write group is anchored to; not called
                when nothing is written.
            emitter: Log emitter for the entries; `None` uses the process default.
            run_id: Pipeline run id so the caller knows it up front; `None` lets the callee mint
                one.
            run_pipeline: Runs the outstanding pipeline stages; required only when stages are
                outstanding.

        Returns:
            A `ReingestionOutcome` describing the completed (or already complete) succession.

        Raises:
            NationalTranspositionNotSupportedError: The prior or new instrument is a national
                transposition; raised before any write.
            ChangeMonitorStateError: The graph is inconsistent, or stages are outstanding and
                `run_pipeline` is `None`.
            SuccessionPersistenceError: A gateway group was refused or not applied, or the stores
                were unreachable.
            AuditTrailUnavailableError: `audit_anchor` could not write its audit row.
            Exception: Whatever `run_pipeline` raises for a stage failure; no succession is written
                and the prior version stays active.

        Side Effects:
            Runs the outstanding pipeline stages, writes progress markers, the new version and the
            succession through the gateway, and emits log entries.
        """
        ...


class WillReingestCall(Protocol):
    """Call shape of `change_monitor.trigger.will_reingest`."""

    def __call__(self, graph: GraphHandle, short_name: str, new_version: str) -> bool:
        """Report whether `trigger_reingestion` would run at least one pipeline stage.

        Args:
            graph: Read handle onto the `{short_name}_native` graph.
            short_name: Instrument short name.
            new_version: Version string of the new version.

        Returns:
            `True` when at least one stage would run; `False` for an already processed or link-only
            re-ingest.

        Raises:
            ChangeMonitorStateError: The graph is inconsistent.
            SuccessionPersistenceError: FalkorDB could not be queried.

        Side Effects:
            Read-only queries on `graph`; writes and logs nothing.
        """
        ...


@dataclass(frozen=True, slots=True)
class ChangeCheckDependencies:
    """Everything `run_change_check_sweep` needs that is not per-request.

    `pipeline` is the catalog pipeline's own stage, adapter and graph-opener bundle: the sweep
    re-uses its stage sequence, so a detected amendment runs Ingestion, Domain Mapper and Company
    Merge.

    Attributes:
        open_single_tenant: Opens the merged `policy_system` graph for a configuration.
        open_native: Opens the `{short_name}_native` graph for a configuration and short name.
        read_tracked_instruments: Enumerates the tracked instruments in the merged graph.
        poll_for_amendments: Polls the tracked instruments for newer consolidated versions.
        trigger_reingestion: Re-ingests one instrument at a new version and records the succession.
        default_adapter: Builds the ingestion adapter used for the re-ingest.
        find_catalog_entry: Resolves a base-act CELEX to its curated `CatalogEntry`, or `None`.
        will_reingest: Reports whether a re-ingest would run any pipeline stage.
        pipeline: The catalog pipeline's dependency bundle whose stage sequence the sweep re-uses.
        graph_write_access: Returns the Graph Write Gateway access for a configuration; raises
            `GraphWriteUnavailableError` when no gateway is installed.
    """

    open_single_tenant: Callable[[ServiceConfig], GraphHandle]
    open_native: Callable[[ServiceConfig, str], GraphHandle]
    read_tracked_instruments: ReadTrackedInstrumentsCall
    poll_for_amendments: PollForAmendmentsCall
    trigger_reingestion: TriggerReingestionCall
    default_adapter: Callable[[], MetadataFetchingAdapter]
    find_catalog_entry: Callable[[str], CatalogEntry | None]
    will_reingest: WillReingestCall
    pipeline: PipelineDependencies
    graph_write_access: Callable[[ServiceConfig], GraphWriteAccess]


def _emit_sweep(
    *,
    outcome: str,
    run_id: str,
    emitter: LogEmitter | None,
    duration_ms: float | None = None,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Emit one `change_check_sweep` log entry.

    Args:
        outcome: `"started"`, `"succeeded"` or `"failed"` (the sweep aborted).
        run_id: The request-scoped run id, passed explicitly; nothing is minted here.
        emitter: Log emitter override; `None` uses the process default.
        duration_ms: Wall time of the sweep so far; omitted on `"started"`.
        extra: Structured fields, such as the abort reason class and the processed count.
    """
    emit_log_entry(
        component=_COMPONENT,
        action=_SWEEP_ACTION,
        outcome=outcome,
        run_id=run_id,
        duration_ms=duration_ms,
        extra=extra,
        emitter=emitter,
    )


def _emit_instrument(
    *,
    run_id: str,
    instrument_id: str,
    outcome: InstrumentCheckOutcomeValue,
    emitter: LogEmitter | None,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Emit one `change_check_instrument` log entry for a classified instrument.

    The entry carries the sweep-level `run_id` explicitly, so a nested run id bound inside
    `poll_for_amendments` or `trigger_reingestion` never leaks onto it.

    Args:
        run_id: The sweep's run id.
        instrument_id: The tracked instrument's `regulatory_instrument_id`; used as the entity id.
        outcome: The bucket this instrument landed in.
        emitter: Log emitter override; `None` uses the process default.
        extra: Structured fields: the enumerated `reason_code` and the `failing_stage` of a failed
            or skipped instrument.
    """
    emit_log_entry(
        component=_COMPONENT,
        action=_INSTRUMENT_ACTION,
        entity_id=instrument_id,
        outcome=outcome,
        run_id=run_id,
        extra=extra,
        emitter=emitter,
    )


def run_change_check_sweep(
    *,
    config: ServiceConfig,
    run_id: str,
    dependencies: ChangeCheckDependencies,
    audit: AuditContext,
    emitter: LogEmitter | None = None,
) -> ChangeCheckResult:
    """Sweep every tracked instrument for amendments and re-ingest any that are found.

    Opens the merged `policy_system` graph once, reads the tracked-instrument set once, polls
    against that same handle, then classifies each tracked instrument: an instrument with a
    finding is re-ingested (Ingestion, Domain Mapper, Company Merge, succession written last);
    the others are `poll_failed`, `not_configured` or `current` by membership in the poll report's
    `failed_ids` and `unconfigured_ids`. A failure of one instrument is reported for that
    instrument and the sweep continues with the next.

    Every re-ingest that actually writes is bracketed by an `ingestion_run.submit` and an
    `ingestion_run.complete` audit row (`trigger='amendment_check'`, the re-ingest's own run id as
    `resource_id`, `audit.actor` as actor). The opening row is fail-closed: if it cannot be written
    the whole sweep aborts, after a `failed` `change_check_sweep` log entry; pairs of instruments
    already re-ingested earlier in the sweep stay recorded, but their outcomes are not returned.

    Args:
        config: The resolved service configuration.
        run_id: The request-scoped run id, minted once by the route. It is threaded unchanged onto
            every log entry this function emits.
        dependencies: The dependency bundle (the production one, or a fake in tests).
        audit: Who triggered the sweep and where the audit rows go.
        emitter: Log emitter override; `None` uses the process default.

    Returns:
        A :class:`ChangeCheckResult` with `run_id` and one :class:`InstrumentCheckOutcome` per
        tracked instrument, in the order `read_tracked_instruments` returned them; empty when no
        instrument is tracked.

    Raises:
        GraphWriteUnavailableError: No Graph Write Gateway is installed; nothing was opened,
            logged or written.
        AuditTrailUnavailableError: An opening audit row could not be written; the sweep stopped
            before that instrument's re-ingest.
        redis.exceptions.RedisError: The graph cannot be read, or the native graph cannot be
            opened; the sweep aborts.
        Exception: Any other error from reading the tracked instruments, polling, or building the
            adapter or opening the native graph for an instrument aborts the sweep. Errors from the
            re-ingest itself are isolated per instrument and reported as `skipped` or
            `reingest_failed`.

    Side Effects:
        Emits `change_check_sweep` log entries (`started`, then `succeeded` or `failed`) and one
        `change_check_instrument` entry per classified instrument. For each re-ingest that writes,
        records the audit pair and writes the new version, progress markers and succession to the
        graphs through the Graph Write Gateway.
    """
    # Resolved first and unconditionally: the succession of even a `finalize` or
    # link-only re-ingest is written through the gateway, so without one nothing may start.
    access = dependencies.graph_write_access(config)
    started = time.perf_counter()
    _emit_sweep(outcome="started", run_id=run_id, emitter=emitter)
    single_tenant = dependencies.open_single_tenant(config)
    tracked = dependencies.read_tracked_instruments(single_tenant)
    poll_report = dependencies.poll_for_amendments(single_tenant)
    celex_by_id = {node.regulatory_instrument_id: node.celex for node in tracked}
    findings_by_id = {finding.regulatory_instrument_id: finding for finding in poll_report.findings}
    failed_ids = set(poll_report.failed_ids)
    unconfigured_ids = set(poll_report.unconfigured_ids)

    outcomes: list[InstrumentCheckOutcome] = []
    for node in tracked:
        instrument_id = node.regulatory_instrument_id
        finding = findings_by_id.get(instrument_id)
        if finding is not None:
            try:
                outcome = _reingest_one(
                    finding,
                    celex_by_id[instrument_id],
                    config=config,
                    single_tenant=single_tenant,
                    dependencies=dependencies,
                    audit=audit,
                    access=access,
                    emitter=emitter,
                )
            except AuditTrailUnavailableError:
                _emit_sweep(
                    outcome="failed",
                    run_id=run_id,
                    emitter=emitter,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    extra={
                        "reason": "AuditTrailUnavailableError",
                        "instruments_processed": len(outcomes),
                    },
                )
                raise
        elif instrument_id in failed_ids:
            outcome = InstrumentCheckOutcome(instrument_id, "poll_failed")
        elif instrument_id in unconfigured_ids:
            outcome = InstrumentCheckOutcome(instrument_id, "not_configured")
        else:
            outcome = InstrumentCheckOutcome(instrument_id, "current")
        _emit_instrument(
            run_id=run_id,
            instrument_id=instrument_id,
            outcome=outcome.outcome,
            emitter=emitter,
            extra=_failure_extra(outcome),
        )
        outcomes.append(outcome)
    _emit_sweep(
        outcome="succeeded",
        run_id=run_id,
        emitter=emitter,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return ChangeCheckResult(run_id=run_id, instruments=tuple(outcomes))


def _reingest_one(
    finding: AmendmentFinding,
    celex: str | None,
    *,
    config: ServiceConfig,
    single_tenant: GraphHandle,
    dependencies: ChangeCheckDependencies,
    audit: AuditContext,
    access: GraphWriteAccess,
    emitter: LogEmitter | None,
) -> InstrumentCheckOutcome:
    """Re-ingest one detected amendment and classify the result.

    Resolves the tracked instrument's base-act CELEX to a curated `CatalogEntry`, then calls
    `trigger_reingestion` with the base-act CELEX as identifier, the entry's short name, the
    finding's `detected_consolidated_celex` as the new version, and the default adapter and native
    graph for that short name. Failure paths handled here:

    * `celex is None`: a defensive guard (a real poll never reports a finding for a node without a
      CELEX); `reingest_failed`.
    * No curated entry for the CELEX (for example a Cellar-fallback ingest): `reingest_failed`
      before any adapter, graph or write is touched.
    * `trigger_reingestion` or the pre-flight catch-up raises: `skipped` for the national
      transposition refusal (its message is surfaced), `reingest_failed` for anything else (only the
      enumerated reason code and stage are surfaced). Never re-raised, so the sweep goes on.

    Args:
        finding: The amendment finding of this tracked instrument.
        celex: The tracked instrument's own `celex`, or `None` when it has none on file.
        config: The resolved service configuration, used to open the native graph and run stages.
        single_tenant: The already opened merged `policy_system` graph that Company Merge writes to.
        dependencies: The dependency bundle.
        audit: Who triggered the sweep and where the audit rows go. A pair is written for a
            re-ingest that runs stages or that writes its succession; an already processed
            re-ingest writes nothing.
        access: The Graph Write Gateway access the sweep resolved first; every succession group
            goes through its gateway.
        emitter: Log emitter override; `None` uses the process default.

    Returns:
        An outcome of `amendment_reingested`, `skipped` or `reingest_failed`.

    Raises:
        AuditTrailUnavailableError: The opening audit row could not be written; this aborts the
            sweep.
        Exception: Errors from `dependencies.default_adapter` or `dependencies.open_native`
            propagate.

    Side Effects:
        May apply a committed but unapplied graph group (catch-up), run pipeline stages, and write
        audit rows and graph groups; see :func:`_audited_reingest`.
    """
    instrument_id = finding.regulatory_instrument_id
    if celex is None:
        return InstrumentCheckOutcome(
            instrument_id,
            "reingest_failed",
            detail="tracked instrument has no celex on file",
        )
    entry = dependencies.find_catalog_entry(celex)
    if entry is None:
        return InstrumentCheckOutcome(
            instrument_id,
            "reingest_failed",
            detail=f"no curated catalog entry for CELEX {celex}",
        )
    adapter = dependencies.default_adapter()
    graph = dependencies.open_native(config, entry.short_name)
    new_version = finding.detected_consolidated_celex
    from ps_service.change_monitor.graph_submission import (  # noqa: PLC0415 -- keeps ps_service.main off Regulatory Change Monitor at import
        ensure_caught_up,
    )

    try:
        # A group committed on an earlier sweep but not yet applied is applied before the graph is
        # classified, so a resume starts at the stage the marker names.
        ensure_caught_up(access.gateway, native_graph_name(entry.short_name), emitter=emitter)
        runs_stages = dependencies.will_reingest(graph, entry.short_name, new_version)
    except Exception as exc:  # noqa: BLE001 -- per-instrument isolation boundary
        return _failed_outcome(instrument_id, exc)
    run_pipeline = _build_pipeline_runner(
        entry,
        new_version,
        config=config,
        adapter=adapter,
        native=graph,
        single_tenant=single_tenant,
        dependencies=dependencies,
        access=access,
        emitter=emitter,
    )
    return _audited_reingest(
        finding,
        celex,
        entry,
        new_version,
        adapter=adapter,
        graph=graph,
        single_tenant_graph_name=config.falkordb_graph,
        run_pipeline=run_pipeline,
        dependencies=dependencies,
        audit=audit,
        access=access,
        eager_opening_row=runs_stages,
        emitter=emitter,
    )


def _build_pipeline_runner(  # noqa: PLR0913 -- one runner's collaborators; no natural grouping
    entry: CatalogEntry,
    new_version: str,
    *,
    config: ServiceConfig,
    adapter: MetadataFetchingAdapter,
    native: GraphHandle,
    single_tenant: GraphHandle,
    dependencies: ChangeCheckDependencies,
    access: GraphWriteAccess,
    emitter: LogEmitter | None,
) -> PipelineRunner:
    """Build the `PipelineRunner` that `trigger_reingestion` runs its stages through.

    `change_monitor` must not import `ps_service.api`, so the shared catalog stage sequence
    (ingest, extract, derive, merge) is handed to it as this callable. The runner checks the
    pipeline configuration first, before any stage runs, runs exactly the requested stage subset
    for the new version's instrument id, reports each completed stage to `on_stage_complete`, and
    always clears the run-status entry. Graph-writing stages write through the Graph Write Gateway
    in `access`, which the sweep resolved once up front because even a link-only re-ingest writes
    its succession through it.

    Args:
        entry: The curated catalog entry of the instrument.
        new_version: The version being ingested; replaces the entry's version.
        config: The resolved service configuration.
        adapter: The ingestion adapter for the re-ingest.
        native: The opened native graph of the instrument.
        single_tenant: The opened merged `policy_system` graph.
        dependencies: The dependency bundle.
        access: The Graph Write Gateway access the sweep resolved.
        emitter: Log emitter override; `None` uses the process default.

    Returns:
        A callable taking `stages`, `run_id` and `on_stage_complete` and returning a
        `PipelineRunResult` with one `StageSummary` per completed stage. Nothing is caught inside
        it: `PipelineStageError`, `IngestionConfigIncompleteError` (config incomplete) and
        `AuditTrailUnavailableError` propagate to the sweep's per-instrument boundary.
    """
    from ps_service.change_monitor.models import (  # noqa: PLC0415 -- keeps ps_service.main off Regulatory Change Monitor at import
        PipelineRunResult,
        StageSummary,
    )

    def _run(
        *, stages: tuple[str, ...], run_id: str, on_stage_complete: Callable[[str], None]
    ) -> PipelineRunResult:
        resolved = _require_ingestion_config(config)
        graphs = _OpenGraphs(
            native=native,
            baseline=dependencies.pipeline.graphs.baseline(config, entry.short_name),
            single_tenant=single_tenant,
        )
        versioned = CatalogEntry(entry.celex, entry.title, entry.short_name, new_version)
        try:
            _, reports = _execute_catalog_stages(
                versioned,
                run_id=run_id,
                resolved=resolved,
                graphs=graphs,
                dependencies=dependencies.pipeline,
                emitter=emitter,
                ingestion_adapter=adapter,
                stages_to_run=stages,
                on_stage_complete=on_stage_complete,
                graph_writes=access,
                trigger="amendment_check",
            )
        finally:
            clear_stage(run_id)
        return PipelineRunResult(
            stages=tuple(StageSummary(report.stage, report.summary) for report in reports)
        )

    return _run


def _failure_extra(outcome: InstrumentCheckOutcome) -> dict[str, object] | None:
    """Return the log fields of a failed instrument (enumerated codes only), else `None`."""
    if outcome.reason_code is None:
        return None
    extra: dict[str, object] = {"reason_code": outcome.reason_code}
    if outcome.failing_stage is not None:
        extra["failing_stage"] = outcome.failing_stage
    return extra


def _failed_outcome(instrument_id: str, exc: Exception) -> InstrumentCheckOutcome:
    """Map a `trigger_reingestion` failure to a `skipped` or `reingest_failed` outcome.

    The `detail` of a `reingest_failed` instrument is the enumerated audit `reason_code`, plus the
    failing pipeline stage when known (`pipeline_stage_failed (stage: derivation)`); the exception
    message is never copied anywhere user-visible. `skipped` keeps `str(exc)`, the national
    transposition refusal's known, domain-level explanation.
    """
    reason_code = _audit_reason_code(exc)
    if type(exc).__name__ == _NATIONAL_TRANSPOSITION_ERROR_NAME:
        return InstrumentCheckOutcome(
            instrument_id, "skipped", detail=str(exc), reason_code=reason_code
        )
    stage = exc.stage if isinstance(exc, PipelineStageError) else None
    return InstrumentCheckOutcome(
        instrument_id,
        "reingest_failed",
        detail=reason_code if stage is None else f"{reason_code} (stage: {stage})",
        reason_code=reason_code,
        failing_stage=stage,
    )


def _reingested_detail(outcome: ReingestionOutcome) -> str:
    """Format `<new id> (<outcome>, <state>)`; an already processed re-ingest omits the state."""
    if outcome.outcome == "already_processed":
        return f"{outcome.new_regulatory_instrument_id} ({outcome.outcome})"
    return f"{outcome.new_regulatory_instrument_id} ({outcome.outcome}, {outcome.state})"


def _reingested_outcome(instrument_id: str, outcome: ReingestionOutcome) -> InstrumentCheckOutcome:
    """Build the `amendment_reingested` outcome, whatever the re-ingest's own state."""
    return InstrumentCheckOutcome(
        instrument_id,
        "amendment_reingested",
        detail=_reingested_detail(outcome),
        reingest_run_id=outcome.run_id,
    )


def _audit_reason_code(exc: Exception) -> IngestionReasonCode:
    """Return the enumerated audit `reason_code` of a failed re-ingest, from the exception type."""
    if type(exc).__name__ == _NATIONAL_TRANSPOSITION_ERROR_NAME:
        return "unsupported_instrument_type"
    return classify_ingestion_failure(exc)


class _OpeningRow:
    """The re-ingest's `ingestion_run.submit` audit row, recorded once, on first use.

    Every group of the succession is anchored to this row's event id. A re-ingest that runs stages
    records it up front (the instance is called before `trigger_reingestion`); one that only writes
    the succession (`finalize`, link-only) records it lazily, just before its first group, so an
    already processed re-ingest, which writes nothing, records nothing. Memoised: every group of a
    re-ingest carries the same id.

    Attributes:
        failure: The fail-closed error a lazy first use raised, so the sweep can tell it from other
            failures; `None` until then.
    """

    def __init__(
        self,
        audit: AuditContext,
        celex: str,
        entry: CatalogEntry,
        reingest_run_id: str,
        emitter: LogEmitter | None,
    ) -> None:
        self._audit = audit
        self._celex = celex
        self._entry = entry
        self._run_id = reingest_run_id
        self._emitter = emitter
        self._event_id: str | None = None
        self.failure: AuditTrailUnavailableError | None = None
        """The fail-closed error a lazy first use raised, so the sweep can tell it from others."""

    @property
    def recorded(self) -> bool:
        """Whether the row was written."""
        return self._event_id is not None

    def __call__(self) -> str:
        """Record the row if it is not recorded yet and return its audit event id.

        Returns:
            The audit event id of the `ingestion_run.submit` row; the same id on every call.

        Raises:
            AuditTrailUnavailableError: The row could not be written (fail-closed); also kept in
                `failure`.

        Side Effects:
            On the first call, writes the `ingestion_run.submit` audit row.
        """
        if self._event_id is None:
            try:
                self._event_id = record_opening_row_with_event_id(
                    self._audit,
                    AuditTarget(
                        INGESTION_RUN_SUBMIT_ACTION, INGESTION_RUN_RESOURCE_TYPE, self._run_id
                    ),
                    component=_COMPONENT,
                    details=submission_audit_entry(
                        celex=self._celex,
                        short_name=self._entry.short_name,  # raw: never normalized
                        trigger="amendment_check",
                    ).details,
                    emitter=self._emitter,
                )
            except AuditTrailUnavailableError as exc:
                self.failure = exc
                raise
        return self._event_id


def _audited_reingest(  # noqa: PLR0913 -- one re-ingest's collaborators; no natural grouping
    finding: AmendmentFinding,
    celex: str,
    entry: CatalogEntry,
    new_version: str,
    *,
    adapter: MetadataFetchingAdapter,
    graph: GraphHandle,
    single_tenant_graph_name: str,
    run_pipeline: PipelineRunner,
    dependencies: ChangeCheckDependencies,
    audit: AuditContext,
    access: GraphWriteAccess,
    eager_opening_row: bool,
    emitter: LogEmitter | None,
) -> InstrumentCheckOutcome:
    """Re-ingest one instrument, bracketed by its `ingestion_run.submit` and `.complete` audit rows.

    The re-ingest's own run id is minted here, passed to `trigger_reingestion` and used as both
    rows' `resource_id`. The opening row is also the audit anchor of every group of the
    succession. When stages run (`eager_opening_row`) it is recorded before `trigger_reingestion`,
    outside the isolation `try`, so its `AuditTrailUnavailableError` aborts the sweep. A re-ingest
    that runs no stage records it lazily, just before its first succession group: an already
    processed one records nothing, any other writes the pair (with an empty stage list). The lazy
    row's `AuditTrailUnavailableError` also aborts the sweep, with nothing of that group written.
    The terminal row is best-effort.

    Args:
        finding: The amendment finding of the instrument.
        celex: The instrument's base-act CELEX.
        entry: The curated catalog entry of the instrument.
        new_version: The version being ingested.
        adapter: The ingestion adapter.
        graph: The opened native graph of the instrument.
        single_tenant_graph_name: Name of the merged single-tenant graph the succession is
            mirrored on.
        run_pipeline: The pipeline runner handed to `trigger_reingestion`.
        dependencies: The dependency bundle.
        audit: Who triggered the sweep and where the audit rows go.
        access: The Graph Write Gateway access the sweep resolved.
        eager_opening_row: Whether stages will run, so the opening row is recorded up front.
        emitter: Log emitter override; `None` uses the process default.

    Returns:
        `amendment_reingested` on success, or the `skipped` / `reingest_failed` outcome of the
        isolated failure.

    Raises:
        AuditTrailUnavailableError: The opening row (eager or lazy) could not be written.

    Side Effects:
        Writes the `ingestion_run.submit` row and, best-effort, the `ingestion_run.complete` row;
        runs the re-ingest, which writes graph groups through the gateway.
    """
    instrument_id = finding.regulatory_instrument_id
    reingest_run_id = str(uuid.uuid4())
    with bind_run_context(reingest_run_id):
        opening = _OpeningRow(audit, celex, entry, reingest_run_id, emitter)
        if eager_opening_row:
            opening()
        try:
            outcome = dependencies.trigger_reingestion(
                celex,
                entry.short_name,
                new_version,
                adapter=adapter,
                graph=graph,
                submitter=access.gateway,
                single_tenant_graph_name=single_tenant_graph_name,
                audit_anchor=opening,
                emitter=emitter,
                run_id=reingest_run_id if eager_opening_row else None,
                run_pipeline=run_pipeline,
            )
        except Exception as exc:
            if exc is opening.failure:
                raise
            if opening.recorded:
                reason = _audit_reason_code(exc)
                _record_terminal_row(audit, reingest_run_id, celex, None, reason, emitter)
            return _failed_outcome(instrument_id, exc)
        if opening.recorded:
            result: dict[str, object] = {
                "regulatory_instrument_id": outcome.new_regulatory_instrument_id,
                "outcome": "fresh",
                "stages": [
                    {"stage": stage.stage, "summary": stage.summary}
                    for stage in outcome.stage_summaries
                ],
            }
            _record_terminal_row(audit, reingest_run_id, celex, result, None, emitter)
    return _reingested_outcome(instrument_id, outcome)


def _record_terminal_row(
    audit: AuditContext,
    reingest_run_id: str,
    celex: str,
    result: dict[str, object] | None,
    reason_code: IngestionReasonCode | None,
    emitter: LogEmitter | None,
) -> None:
    """Write the re-ingest's `ingestion_run.complete` audit row, best-effort.

    A write failure is logged and swallowed.
    """
    entry = completion_audit_entry(
        status="failed" if reason_code is not None else "succeeded",
        celex=celex,
        trigger="amendment_check",
        result=result,
        reason_code=reason_code,
    )
    record_follow_up_row(
        audit,
        AuditTarget(INGESTION_RUN_COMPLETE_ACTION, INGESTION_RUN_RESOURCE_TYPE, reingest_run_id),
        component=_COMPONENT,
        outcome=entry.outcome,
        details=entry.details,
        emitter=emitter,
    )


# --- default wiring (every change_monitor import below is function-local) ---


def build_default_change_check_dependencies() -> ChangeCheckDependencies:
    """Wire the real Regulatory Change Monitor entry points into a `ChangeCheckDependencies`.

    `read_tracked_instruments`, `poll_for_amendments`, `trigger_reingestion` and `will_reingest`
    are imported function-locally so importing `ps_service.main` never loads Regulatory Change
    Monitor at module load. The graph openers and the default ingestion adapter are reused from
    `ingestion_orchestration`, whose own top-level imports do not reference `change_monitor`.

    Returns:
        A :class:`ChangeCheckDependencies` bound to the production entry points, the production
        catalog pipeline dependencies and the process's Graph Write Gateway access.
    """
    from ps_service.change_monitor.graph_reader import (  # noqa: PLC0415 -- keeps ps_service.main off Regulatory Change Monitor at import
        read_tracked_instruments,
    )
    from ps_service.change_monitor.poll import (  # noqa: PLC0415 -- keeps ps_service.main off Regulatory Change Monitor at import
        poll_for_amendments,
    )
    from ps_service.change_monitor.trigger import (  # noqa: PLC0415 -- keeps ps_service.main off Regulatory Change Monitor at import
        trigger_reingestion,
        will_reingest,
    )

    return ChangeCheckDependencies(
        pipeline=build_default_pipeline_dependencies(),
        open_single_tenant=_open_single_tenant_graph,
        open_native=_open_native_graph,
        read_tracked_instruments=read_tracked_instruments,
        poll_for_amendments=poll_for_amendments,
        trigger_reingestion=trigger_reingestion,
        default_adapter=_default_ingestion_adapter,
        find_catalog_entry=find_by_celex,
        will_reingest=will_reingest,
        graph_write_access=graph_write_access,
    )
