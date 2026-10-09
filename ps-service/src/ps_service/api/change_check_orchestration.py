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

Slice 5 (PLAN.md §4) wires ``_safe_reason`` (D11) into that generic
``reingest_failed`` branch: its ``detail`` is scrubbed and length-capped,
reusing the exact shared scrubber ``ingestion_orchestration.
_classify_stage_failure`` already uses (``error_handlers._scrub_text``) --
this endpoint's per-instrument ``detail`` is returned as normal
200-response body content, never through an ``ApiError``/exception
handler, so scrubbing it is this module's own responsibility.

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

from ps_service.api.catalog import find_by_celex
from ps_service.api.error_handlers import (
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  -- shared scrubber; PLAN.md §1 D11 sanctions reuse, mirrors ingestion_orchestration.py:42-44's own precedent
)
from ps_service.api.ingestion_orchestration import (
    _default_ingestion_adapter,  # pyright: ignore[reportPrivateUsage]  -- shared graph/adapter factory; reused per PLAN.md §0.4/D9's main.py:22-25 precedent
    _open_native_graph,  # pyright: ignore[reportPrivateUsage]  -- see above
    _open_single_tenant_graph,  # pyright: ignore[reportPrivateUsage]  -- see above
    classify_ingestion_failure,
)
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

    from ps_service.api.catalog import CatalogEntry
    from ps_service.api.ingestion_orchestration import GraphHandle
    from ps_service.audit.emit import AuditContext
    from ps_service.change_monitor.models import (
        AmendmentFinding,
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

# D11: matches `ingestion_orchestration._STAGE_REASON_MAX_LEN` exactly.
_REASON_MAX_LEN = 300


def _safe_reason(exc: Exception) -> str:
    """Scrub and length-cap an exception's message for a `reingest_failed` `detail` (D11).

    Mirrors `ingestion_orchestration._classify_stage_failure`'s own shape
    exactly, reusing the same shared `_scrub_text` scrubber -- this
    endpoint's `detail` is returned as normal 200-response body content,
    never through an `ApiError`/exception handler, so scrubbing it here is
    this module's own responsibility (not inherited for free).

    Args:
        exc: The exception `trigger_reingestion` raised.

    Returns:
        `f"{type(exc).__name__}: {exc}"`, scrubbed of filesystem paths, the
        repo/home dirs, `host:port` tokens, and URLs, then truncated to
        `_REASON_MAX_LEN` characters.
    """
    return _scrub_text(f"{type(exc).__name__}: {exc}")[:_REASON_MAX_LEN]


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

    def __call__(
        self,
        identifier: str,
        short_name: str,
        new_version: str,
        *,
        adapter: MetadataFetchingAdapter,
        graph: GraphHandle,
        emitter: LogEmitter | None = None,
        run_id: str | None = None,
    ) -> ReingestionOutcome:
        """Re-ingest `identifier` as `new_version` and record its succession."""
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
    seam shape (PLAN.md §2). ``open_native``/``trigger_reingestion``/
    ``default_adapter`` go unused until Slice 3 wires the ``finding_ids``
    branch for real -- present now so this bundle's shape is stable across
    every later slice, no repeated construction-site edits (mirrors Slice 1's
    "declare all six outcome values up front" rationale for the wire model).
    """

    open_single_tenant: Callable[[ServiceConfig], GraphHandle]
    open_native: Callable[[ServiceConfig, str], GraphHandle]
    read_tracked_instruments: ReadTrackedInstrumentsCall
    poll_for_amendments: PollForAmendmentsCall
    trigger_reingestion: TriggerReingestionCall
    default_adapter: Callable[[], MetadataFetchingAdapter]
    find_catalog_entry: Callable[[str], CatalogEntry | None]
    will_reingest: WillReingestCall


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
    """
    emit_log_entry(
        component=_COMPONENT,
        action=_INSTRUMENT_ACTION,
        entity_id=instrument_id,
        outcome=outcome,
        run_id=run_id,
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
    ``poll_failed`` / ``not_configured`` / ``current`` (this slice). An
    instrument whose id is one of ``poll_report.findings``'s own ids (an
    amendment was detected) hits the ``finding_ids`` branch below, a
    structural stub this slice (PLAN.md §4 Slice 2's own note) -- Slice 3
    replaces it with a real ``_reingest_one`` call implementing D2-D7's full
    call contract; no Slice-2 test scripts a non-empty ``findings`` tuple,
    so this branch is present but unreached.

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
            run_id=run_id, instrument_id=instrument_id, outcome=outcome.outcome, emitter=emitter
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
            `dependencies.open_native`.
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
        fresh = dependencies.will_reingest(graph, entry.short_name, new_version)
    except Exception as exc:  # noqa: BLE001 -- per-instrument isolation boundary, AC-BI-006/007
        return _failed_outcome(instrument_id, exc)
    if not fresh:
        # `resume` / `already_processed` ingest nothing, so there is no ingestion to audit
        # (CHANGES F-4); the supersession log entry already covers `resume`.
        try:
            outcome = dependencies.trigger_reingestion(
                celex, entry.short_name, new_version, adapter=adapter, graph=graph
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
        dependencies=dependencies,
        audit=audit,
        emitter=emitter,
    )


def _failed_outcome(instrument_id: str, exc: Exception) -> InstrumentCheckOutcome:
    """Map a `trigger_reingestion` failure to `skipped` (D10) or `reingest_failed` (D6/D11)."""
    if type(exc).__name__ == _NATIONAL_TRANSPOSITION_ERROR_NAME:
        return InstrumentCheckOutcome(instrument_id, "skipped", detail=str(exc))
    return InstrumentCheckOutcome(instrument_id, "reingest_failed", detail=_safe_reason(exc))


def _reingested_outcome(instrument_id: str, outcome: ReingestionOutcome) -> InstrumentCheckOutcome:
    """The `amendment_reingested` outcome (D7), whatever the re-ingest's own state."""
    return InstrumentCheckOutcome(
        instrument_id,
        "amendment_reingested",
        detail=f"{outcome.new_regulatory_instrument_id} ({outcome.outcome})",
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
                run_id=reingest_run_id,
            )
        except Exception as exc:  # noqa: BLE001 -- per-instrument isolation boundary, AC-BI-006/007
            reason = _audit_reason_code(exc)
            _record_terminal_row(audit, reingest_run_id, celex, None, reason, emitter)
            return _failed_outcome(instrument_id, exc)
        # Counts are 0/0/0 on purpose: the sweep re-runs ONLY the Ingestion stage (+ the
        # SUPERSEDED_BY write); Domain Mapper and Company Merge do not run, so it writes no
        # Obligation or Capability (verified against the graph in AC-BI-016's test). The
        # doc/code gap with UC-4 (amendment absorption "Ingestion -> Domain Mapper -> Company
        # Merge") is tracked in GitHub issue #201; once the sweep runs the full pipeline, take
        # the counts from its MergeResult here.
        result: dict[str, object] = {
            "regulatory_instrument_id": outcome.new_regulatory_instrument_id,
            "outcome": "fresh",
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
        open_single_tenant=_open_single_tenant_graph,
        open_native=_open_native_graph,
        read_tracked_instruments=read_tracked_instruments,
        poll_for_amendments=poll_for_amendments,
        trigger_reingestion=trigger_reingestion,
        default_adapter=_default_ingestion_adapter,
        find_catalog_entry=find_by_celex,
        will_reingest=will_reingest,
    )
