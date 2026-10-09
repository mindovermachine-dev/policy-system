"""REST-boundary glue for ``POST /change-checks`` (issue #73, PLAN.md §4).

Mirrors ``restore_orchestration.py``'s shape: an injection seam
(``ChangeCheckDependencies``) and ``build_default_change_check_dependencies``,
which wires the real ``ps_service.change_monitor.*`` entry points via
**function-local** imports so that importing ``ps_service.main`` never
transitively loads Regulatory Change Monitor at module load (M6 --
PLAN.md §0.6/D9). ``run_change_check_sweep`` is the thin wrapper the
``POST /change-checks`` route calls.

Slice 2 (PLAN.md §4) wired the real dependency bundle and D2's algorithm:
open the merged ``policy_system`` graph once, read the tracked-instrument
set from it exactly once, call ``poll_for_amendments`` against that same
graph handle, then classify each tracked instrument into ``current`` /
``poll_failed`` / ``not_configured`` by set membership against the
returned ``PollReport``'s own ``failed_ids``/``unconfigured_ids`` buckets.

Slice 3 (PLAN.md §4) wires the remaining ``finding_ids`` branch: an
instrument whose id is one of the report's ``findings`` is handed to
``_reingest_one``, which implements D2-D7's full call contract --
``find_catalog_entry(celex)`` resolves the finding's base-act CELEX (read
from the ``celex_by_id`` map built here, per D2/D3) to a curated
``CatalogEntry``; ``trigger_reingestion`` is then called with the exact
``identifier``/``short_name``/``new_version``/``adapter``/``graph``
argument contract D4 specifies.

Slice 4 (PLAN.md §4) adds the D10 isolation boundary around that
``trigger_reingestion`` call: an exception whose class is literally named
``NationalTranspositionNotSupportedError`` (matched by name, never
imported) becomes ``skipped``; any other exception becomes
``reingest_failed`` -- never propagated past this boundary, so the sweep
always continues to the next tracked instrument (AC-BI-006/AC-BI-007).

A ``reingest_failed`` instrument's ``detail`` is never exception text: it is the
enumerated audit ``reason_code``, plus the failing pipeline stage when one is
known (``pipeline_stage_failed (stage: derivation)``). The full failure detail is
logged server-side only.

A detected amendment runs the SAME stage sequence the catalog pipeline uses --
Ingestion, Domain Mapper (extract, derive), Company Merge -- through a
``PipelineRunner`` built here (:func:`_build_pipeline_runner`) and injected into
``trigger_reingestion`` (``change_monitor`` must not import ``ps_service.api``).
The runner checks the pipeline configuration first (missing chat model, embed model
or merge threshold is a ``reingest_failed`` before any graph write), and the
succession is written only after the merge stage returned; a failed stage leaves the
prior version ``active`` and the next sweep re-runs only the missing stages.

Slice 6 (PLAN.md §4, per ``CHANGES.md``'s scope reduction -- the ps-cli
display work originally planned for this slice was moved into and
completed in Slice 2) wires ``_emit_sweep``/``_emit_instrument`` (D8,
AC-BI-008 full): the sweep mints no id of its own -- it reuses the
``run_id`` the route's ``provide_run_id`` binding already passed in --
and every log entry this module emits carries that ``run_id`` via an
**explicit** ``run_id=`` argument, never relying on ``contextvars``
inheritance into ``poll_for_amendments``/``trigger_reingestion`` (D8's own
citation of why that would be wrong: those calls may internally emit
their own entries under their own, separately-bound, nested run ids).
Mirrors ``ingestion_orchestration._emit_run``'s shape exactly.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from ps_service.api.catalog import CatalogEntry, find_by_celex
from ps_service.api.errors import PipelineStageError
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    _default_ingestion_adapter,  # pyright: ignore[reportPrivateUsage]  -- shared graph/adapter factory; reused per PLAN.md §0.4/D9's main.py:22-25 precedent
    _execute_catalog_stages,  # pyright: ignore[reportPrivateUsage]  -- the shared stage sequence the sweep re-uses
    _open_native_graph,  # pyright: ignore[reportPrivateUsage]  -- see above
    _open_single_tenant_graph,  # pyright: ignore[reportPrivateUsage]  -- see above
    _OpenGraphs,  # pyright: ignore[reportPrivateUsage]  -- see above
    _require_ingestion_config,  # pyright: ignore[reportPrivateUsage]  -- config-completeness guard, run first by the runner (AC-BI-006)
    build_default_pipeline_dependencies,
    classify_ingestion_failure,
)
from ps_service.api.run_status import clear_stage
from ps_service.audit.emit import AuditTarget, record_follow_up_row, record_opening_row
from ps_service.audit.errors import AuditTrailUnavailableError
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
    from ps_service.logging import LogEmitter

# D8: mirrors ingestion_orchestration.py's own `_COMPONENT`/action-constant shape.
_COMPONENT = "api"
_SWEEP_ACTION = "change_check_sweep"
_INSTRUMENT_ACTION = "change_check_instrument"

# D10: matched by name (not import) so this module never imports
# `ps_service.change_monitor.errors` at load time -- mirrors
# `error_handlers._SAFE_VERBATIM_NAMES`'s own established idiom for the
# identical cross-component situation.
_NATIONAL_TRANSPOSITION_ERROR_NAME = "NationalTranspositionNotSupportedError"

# The six outcome buckets a tracked instrument can land in (PLAN.md §1 D6/D7).
# Declared here, not re-derived from `api.models.InstrumentCheckOutcomeBody`,
# since this module's dataclasses are internal plumbing (mirrors
# `restore_orchestration`'s `RestoreOutcome`/`RestorationStageOutcome` split
# from their own `api.models` Pydantic counterparts) -- this slice exercises
# only `current`/`poll_failed`/`not_configured`; the other three are
# exercised for real starting Slice 3.
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
    """One tracked instrument's classification from a change-check sweep."""

    instrument_id: str
    outcome: InstrumentCheckOutcomeValue
    detail: str | None = None
    reingest_run_id: str | None = None
    reason_code: IngestionReasonCode | None = None
    failing_stage: str | None = None


@dataclass(frozen=True, slots=True)
class ChangeCheckResult:
    """Result of one change-check sweep: the run id and every tracked instrument's outcome."""

    run_id: str
    instruments: tuple[InstrumentCheckOutcome, ...]


# --- injection seam (PLAN.md §1 D9, mirrors PipelineDependencies/RestoreDependencies) ---


class ReadTrackedInstrumentsCall(Protocol):
    """Call shape of ``change_monitor.graph_reader.read_tracked_instruments``."""

    def __call__(self, graph: GraphHandle) -> tuple[TrackedInstrumentNode, ...]:
        """Enumerate every active, external `regulation`/`directive` instrument in `graph`."""
        ...


class PollForAmendmentsCall(Protocol):
    """Call shape of ``change_monitor.poll.poll_for_amendments``."""

    def __call__(self, graph: GraphHandle, *, emitter: LogEmitter | None = None) -> PollReport:
        """Poll every tracked instrument in `graph` for a newer consolidated version."""
        ...


class TriggerReingestionCall(Protocol):
    """Call shape of ``change_monitor.trigger.trigger_reingestion``."""

    def __call__(  # noqa: PLR0913 -- mirrors trigger_reingestion's signature exactly
        self,
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
        """Re-ingest `identifier` as `new_version` through the full pipeline; link it."""
        ...


class WillReingestCall(Protocol):
    """Call shape of ``change_monitor.trigger.will_reingest`` (issue #195)."""

    def __call__(self, graph: GraphHandle, short_name: str, new_version: str) -> bool:
        """Whether ``trigger_reingestion`` would run a real re-ingest (read-only)."""
        ...


@dataclass(frozen=True, slots=True)
class ChangeCheckDependencies:
    """Everything ``run_change_check_sweep`` needs that is not per-request.

    Mirrors ``PipelineDependencies``/``RestoreDependencies``'s own injection-
    seam shape (PLAN.md §2). ``pipeline`` is the catalog pipeline's own
    stage/adapter/graph-opener bundle: the sweep re-uses its stage sequence, so a
    detected amendment runs Ingestion -> Domain Mapper -> Company Merge (UC-4).
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


def _emit_sweep(
    *,
    outcome: str,
    run_id: str,
    emitter: LogEmitter | None,
    duration_ms: float | None = None,
    extra: Mapping[str, object] | None = None,
) -> None:
    """Emit one ``change_check_sweep`` log entry (D8, AC-BI-008 full).

    Mirrors ``ingestion_orchestration._emit_run``'s shape: ``run_id`` is
    always passed explicitly, never left to ``contextvars`` inheritance.

    Args:
        outcome: ``"started"`` / ``"succeeded"`` / ``"failed"`` (aborted, issue #195).
        run_id: The request-scoped run id (bound by ``provide_run_id``,
            passed into ``run_change_check_sweep`` unchanged -- this
            function mints nothing of its own).
        emitter: Optional explicit emitter; otherwise the process default.
        duration_ms: Wall time for the sweep so far (omitted on ``"started"``).
        extra: Optional structured fields (the abort reason class and processed count).
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
    """Emit one ``change_check_instrument`` log entry (D8, AC-BI-008 full).

    One entry per tracked instrument the sweep classifies, ``entity_id`` set
    to that instrument's own id, carrying the *same* sweep-level ``run_id``
    ``_emit_sweep`` used -- explicit, never inherited via ``contextvars``,
    so a nested id ``poll_for_amendments``/``trigger_reingestion`` may bind
    internally never leaks onto this entry (D8).

    Args:
        run_id: The sweep's own run id (unchanged from ``run_change_check_sweep``).
        instrument_id: The tracked instrument's ``regulatory_instrument_id``.
        outcome: The bucket this instrument landed in.
        emitter: Optional explicit emitter; otherwise the process default.
        extra: Optional structured fields: the enumerated ``reason_code`` and the
            ``failing_stage`` of a ``reingest_failed`` / ``skipped`` instrument.
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
    """Sweep every tracked instrument for amendments and re-ingest any found.

    D2's algorithm (PLAN.md §1): open the merged ``policy_system`` graph
    once, read the tracked-instrument set from it exactly once, call
    ``poll_for_amendments`` against that same graph handle, then classify
    each tracked instrument by set membership against the returned
    ``PollReport``'s own ``failed_ids``/``unconfigured_ids`` buckets --
    ``poll_failed`` / ``not_configured`` / ``current``. An instrument whose id
    is one of ``poll_report.findings``'s own ids (an amendment was detected) goes to
    ``_reingest_one``, which runs the full UC-4 re-ingest (Ingestion, Domain Mapper,
    Company Merge) and records the succession last; a failure of one instrument is
    reported for that instrument and the sweep continues with the next.

    Slice 6 (D8, AC-BI-008 full) wraps this with ``_emit_sweep``'s
    ``"started"``/``"succeeded"`` pair and one ``_emit_instrument`` entry per
    tracked instrument, all carrying this ``run_id`` explicitly.

    Issue #195: every re-ingest that actually runs is bracketed by an
    ``ingestion_run.submit`` / ``ingestion_run.complete`` audit pair
    (``trigger='amendment_check'``, the re-ingest's own run id as ``resource_id``,
    ``audit.actor`` as actor; see :func:`_reingest_one`). The opening row is
    FAIL-CLOSED: if it cannot be written the WHOLE sweep aborts with
    ``AuditTrailUnavailableError`` (a ``change_check_sweep`` ``failed`` log entry
    is emitted first); the pairs of instruments already re-ingested earlier in
    that sweep stay recorded, but their outcomes are not returned to the caller.

    Args:
        config: The resolved service configuration.
        run_id: The request-scoped run id (bound by ``provide_run_id``).
            Minted once by the route, never re-minted here -- this function
            only threads it through, explicitly, onto every log entry it
            emits (D8).
        dependencies: The injected dependency bundle (the production bundle
            in production; a fake in fast tests).
        audit: Who triggered the sweep and where the audit rows go.
        emitter: Optional explicit log emitter; otherwise the process default.

    Returns:
        A :class:`ChangeCheckResult` carrying ``run_id`` and one
        :class:`InstrumentCheckOutcome` per tracked instrument, in
        ``read_tracked_instruments``'s own returned order.

    Raises:
        AuditTrailUnavailableError: An opening audit row could not be written; the sweep
            stopped before that instrument's re-ingest.
    """
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
    emitter: LogEmitter | None,
) -> InstrumentCheckOutcome:
    """Re-ingest one detected amendment (PLAN.md §1 D2-D7's full call contract).

    Resolves ``finding``'s base-act CELEX (``celex``, read from the
    ``celex_by_id`` map :func:`run_change_check_sweep` builds, per D2/D3) to
    a curated :class:`CatalogEntry` via ``dependencies.find_catalog_entry``,
    then calls ``dependencies.trigger_reingestion`` with the exact D4
    argument contract: ``identifier`` is the base-act ``celex`` (the same
    value used for the catalog lookup), ``short_name``/``new_version`` come
    from the resolved entry / the finding's own
    ``detected_consolidated_celex``, and ``adapter``/``graph`` come from
    ``dependencies.default_adapter()``/``dependencies.open_native(config,
    entry.short_name)``.

    Four failure paths are handled explicitly here (D5/D6/D10/D11):

    * ``celex is None`` -- defensive only. Per PLAN.md §0.1,
      ``poll_for_amendments`` never produces a finding for a tracked node
      whose ``celex`` is ``None`` (``poll.py``'s own `_poll_one` returns
      ``not_configured`` before ever classifying such a node), so this
      branch should be structurally unreachable from a real
      ``PollReport`` -- kept as a guard in case that guarantee is ever
      weakened by a future, unrelated change to ``poll.py``.
    * ``entry is None`` -- ``find_by_celex`` has no curated entry for a
      Cellar-fallback-ingested instrument's CELEX (PLAN.md §0.5). D5:
      folded into ``reingest_failed``, never a crash, and neither
      ``trigger_reingestion`` nor ``open_native``/``default_adapter`` is
      ever called in this case (the short-circuit happens before any of
      them, matching D5's "before any write" framing).
    * ``trigger_reingestion`` raises an exception whose class is literally
      named ``NationalTranspositionNotSupportedError`` -- D10: matched by
      name (never imported, mirrors `error_handlers.is_safe_verbatim`'s own
      idiom for the identical cross-component situation) -- ``skipped``,
      `detail` is `str(exc)` verbatim (the message is a known, safe,
      domain-level explanation, not an internal leak).
    * ``trigger_reingestion`` raises any other exception -- D6/D11:
      ``reingest_failed``, `detail` is `_safe_reason(exc)` (scrubbed via the
      shared `error_handlers._scrub_text`, length-capped to
      `_REASON_MAX_LEN`). Neither case re-raises past this boundary -- the
      sweep always continues to its next tracked instrument
      (AC-BI-006/AC-BI-007).

    Args:
        finding: The one `AmendmentFinding` this tracked instrument
            produced.
        celex: The tracked instrument's own `celex` (from `celex_by_id`),
            or `None` if somehow absent (see above).
        config: The resolved service configuration, passed through to
            `dependencies.open_native` and the pipeline runner.
        single_tenant: The already-opened merged `policy_system` graph (Company Merge writes to it).
        dependencies: The injected dependency bundle.
        audit: Who triggered the sweep and where the audit rows go (issue #195). A pair is
            written only when ``dependencies.will_reingest`` says a real re-ingest will run
            (``fresh``) and a catalog entry resolved (D-G); the opening row's
            ``AuditTrailUnavailableError`` propagates out of this function (CHANGES F-5).
        emitter: Optional explicit log emitter.

    Returns:
        `("amendment_reingested", ...)` on a successful `trigger_reingestion`
        call (D7: covers `fresh`/`resume`/`already_processed` alike,
        distinguished only by `detail`/`reingest_run_id`), `("skipped", ...)`
        on the national-transposition guard (D10), or `("reingest_failed",
        ...)` for every other failure path (D5/D6/D11).
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
    try:
        runs_stages = dependencies.will_reingest(graph, entry.short_name, new_version)
    except Exception as exc:  # noqa: BLE001 -- per-instrument isolation boundary, AC-BI-006/007
        return _failed_outcome(instrument_id, exc)
    run_pipeline = _build_pipeline_runner(
        entry,
        new_version,
        config=config,
        adapter=adapter,
        native=graph,
        single_tenant=single_tenant,
        dependencies=dependencies,
        emitter=emitter,
    )
    if not runs_stages:
        # No pipeline stage will run (`already_processed`, or a link-only resume), so there is
        # no ingestion to audit (CHANGES F-4); the supersession log entry covers the link.
        try:
            outcome = dependencies.trigger_reingestion(
                celex,
                entry.short_name,
                new_version,
                adapter=adapter,
                graph=graph,
                single_tenant=single_tenant,
                emitter=emitter,
                run_pipeline=run_pipeline,
            )
        except Exception as exc:  # noqa: BLE001 -- per-instrument isolation boundary
            return _failed_outcome(instrument_id, exc)
        return _reingested_outcome(instrument_id, outcome)
    return _audited_reingest(
        finding,
        celex,
        entry,
        new_version,
        adapter=adapter,
        graph=graph,
        single_tenant=single_tenant,
        run_pipeline=run_pipeline,
        dependencies=dependencies,
        audit=audit,
        emitter=emitter,
    )


def _build_pipeline_runner(
    entry: CatalogEntry,
    new_version: str,
    *,
    config: ServiceConfig,
    adapter: MetadataFetchingAdapter,
    native: GraphHandle,
    single_tenant: GraphHandle,
    dependencies: ChangeCheckDependencies,
    emitter: LogEmitter | None,
) -> PipelineRunner:
    """Build the `PipelineRunner` `trigger_reingestion` runs its stages through.

    `change_monitor` must not import `ps_service.api`, so the shared stage sequence
    (`ingestion_orchestration._execute_catalog_stages`: ingest, extract, derive, merge) is
    handed to it as this callable. The runner checks the pipeline config FIRST, before the
    baseline graph is opened or any stage runs (AC-BI-006), runs exactly the requested
    stage subset for `new_version`'s instrument id, reports each completed stage to
    `on_stage_complete`, and always clears the `run_status` entry. Nothing is caught:
    a `PipelineStageError` or `IngestionConfigIncompleteError` propagates to the sweep's
    per-instrument boundary.
    """
    from ps_service.change_monitor.models import (  # noqa: PLC0415 -- M6: keeps ps_service.main off Regulatory Change Monitor at import
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
            )
        finally:
            clear_stage(run_id)
        return PipelineRunResult(
            stages=tuple(StageSummary(report.stage, report.summary) for report in reports)
        )

    return _run


def _failure_extra(outcome: InstrumentCheckOutcome) -> dict[str, object] | None:
    """The structured log fields of a failed instrument (enumerated codes only), else `None`."""
    if outcome.reason_code is None:
        return None
    extra: dict[str, object] = {"reason_code": outcome.reason_code}
    if outcome.failing_stage is not None:
        extra["failing_stage"] = outcome.failing_stage
    return extra


def _failed_outcome(instrument_id: str, exc: Exception) -> InstrumentCheckOutcome:
    """Map a `trigger_reingestion` failure to `skipped` (D10) or `reingest_failed` (AC-BI-007).

    The user-visible `detail` of a `reingest_failed` instrument is the enumerated audit
    `reason_code`, plus the failing pipeline stage (a fixed set of stage names) when one is
    known: `pipeline_stage_failed (stage: derivation)`. The exception message is never copied
    anywhere user-visible -- a stage failure's full detail is already logged server-side by
    `ingestion_orchestration._classify_stage_failure`. `skipped` keeps `str(exc)`: the
    national-transposition guard's message is a known, safe, domain-level explanation.
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
    """`<new id> (superseded, <state>)` -- the state shows a `resume` / `repair` / `finalize`."""
    if outcome.outcome == "already_processed":
        return f"{outcome.new_regulatory_instrument_id} ({outcome.outcome})"
    return f"{outcome.new_regulatory_instrument_id} ({outcome.outcome}, {outcome.state})"


def _reingested_outcome(instrument_id: str, outcome: ReingestionOutcome) -> InstrumentCheckOutcome:
    """The `amendment_reingested` outcome (D7), whatever the re-ingest's own state."""
    return InstrumentCheckOutcome(
        instrument_id,
        "amendment_reingested",
        detail=_reingested_detail(outcome),
        reingest_run_id=outcome.run_id,
    )


def _audit_reason_code(exc: Exception) -> IngestionReasonCode:
    """Enumerated audit `reason_code` for a failed re-ingest (type only, AC-BI-010)."""
    if type(exc).__name__ == _NATIONAL_TRANSPOSITION_ERROR_NAME:
        return "unsupported_instrument_type"
    return classify_ingestion_failure(exc)


def _audited_reingest(  # noqa: PLR0913 -- one re-ingest's collaborators; no natural grouping
    finding: AmendmentFinding,
    celex: str,
    entry: CatalogEntry,
    new_version: str,
    *,
    adapter: MetadataFetchingAdapter,
    graph: GraphHandle,
    single_tenant: GraphHandle,
    run_pipeline: PipelineRunner,
    dependencies: ChangeCheckDependencies,
    audit: AuditContext,
    emitter: LogEmitter | None,
) -> InstrumentCheckOutcome:
    """Re-ingest one instrument between its `ingestion_run.submit` / `.complete` audit rows.

    The re-ingest's own run id is minted here, passed to ``trigger_reingestion`` and used as both
    rows' ``resource_id`` (AC-BI-007). The opening row sits OUTSIDE the isolation ``try`` so its
    ``AuditTrailUnavailableError`` aborts the sweep (CHANGES F-5, AC-BI-011); the terminal row is
    best-effort (AC-BI-015).
    """
    instrument_id = finding.regulatory_instrument_id
    reingest_run_id = str(uuid.uuid4())
    with bind_run_context(reingest_run_id):
        record_opening_row(
            audit,
            AuditTarget(INGESTION_RUN_SUBMIT_ACTION, INGESTION_RUN_RESOURCE_TYPE, reingest_run_id),
            component=_COMPONENT,
            details=submission_audit_entry(
                celex=celex,
                short_name=entry.short_name,  # raw catalog value: the sweep never normalizes (#193)
                trigger="amendment_check",
            ).details,
            emitter=emitter,
        )
        try:
            outcome = dependencies.trigger_reingestion(
                celex,
                entry.short_name,
                new_version,
                adapter=adapter,
                graph=graph,
                single_tenant=single_tenant,
                emitter=emitter,
                run_id=reingest_run_id,
                run_pipeline=run_pipeline,
            )
        except Exception as exc:  # noqa: BLE001 -- per-instrument isolation boundary, AC-BI-006/007
            reason = _audit_reason_code(exc)
            _record_terminal_row(audit, reingest_run_id, celex, None, reason, emitter)
            return _failed_outcome(instrument_id, exc)
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
    """Write the re-ingest's `ingestion_run.complete` row; BEST-EFFORT (AC-BI-015)."""
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


# --- default wiring (M6 -- every change_monitor import below is function-local) ---


def build_default_change_check_dependencies() -> ChangeCheckDependencies:
    """Wire the real Regulatory Change Monitor entry points into a bundle.

    ``read_tracked_instruments``/``poll_for_amendments``/``trigger_reingestion``
    are imported **function-locally** (M6 -- PLAN.md §0.6/D9) so importing
    ``ps_service.main`` never transitively loads Regulatory Change Monitor at
    module load. ``_open_native_graph``/``_open_single_tenant_graph``/
    ``_default_ingestion_adapter`` are reused, module-level, from
    ``ingestion_orchestration`` (D9's citation of the existing
    ``main.py:22-25``/``ingestion_orchestration.py:42-44``
    ``pyright: ignore[reportPrivateUsage]`` precedent) -- that module's own
    top-level imports contain no ``ps_service.change_monitor`` reference, so
    importing it module-level introduces no M6 violation.

    Returns:
        A :class:`ChangeCheckDependencies` bound to the production
        Regulatory Change Monitor entry points.
    """
    from ps_service.change_monitor.graph_reader import (  # noqa: PLC0415 -- M6: keeps ps_service.main off Regulatory Change Monitor at import
        read_tracked_instruments,
    )
    from ps_service.change_monitor.poll import (  # noqa: PLC0415 -- M6: keeps ps_service.main off Regulatory Change Monitor at import
        poll_for_amendments,
    )
    from ps_service.change_monitor.trigger import (  # noqa: PLC0415 -- M6: keeps ps_service.main off Regulatory Change Monitor at import
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
    )
