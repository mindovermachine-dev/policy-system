"""`ps_service.change_monitor` core types.

The shapes `poll.py` / `trigger.py` / `graph_reader.py` build and consume
internally. All plain frozen dataclasses (PLAN_REVIEWED.md §2 "Public
surface"): internal pipeline plumbing, nothing crosses a component boundary
(`poll_for_amendments` / `trigger_reingestion` are in-process calls), so no
Pydantic. `ConsolidatedVersionInfo` deliberately lives in
`cellar_consolidated.py`, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date


@dataclass(frozen=True, slots=True)
class TrackedInstrumentNode:
    """One active, external `regulation`/`directive` row read from `policy_system`.

    `effective_date` is the ISO string exactly as Ingestion stored it
    (`date.isoformat()`); `celex` is `None` for a node ingested before the
    optional `celex` property existed and not yet re-seeded.
    """

    regulatory_instrument_id: str
    celex: str | None
    instrument_type: str
    effective_date: str


@dataclass(frozen=True, slots=True)
class PriorInstrument:
    """The single active prior `RegulatoryInstrument` a new version supersedes.

    Identified by `succession.find_prior_instrument` with the deterministic
    lookup (PLAN_REVIEWED.md §0): the `status='active'` node that is neither
    the new node nor already superseded into it. `instrument_type` is carried
    so `trigger_reingestion`'s AC-010 guard can reject a
    `national_transposition` prior without a second read.
    """

    id: str
    instrument_type: str


@dataclass(frozen=True, slots=True)
class AmendmentFinding:
    """One `amendment_detected` result row from `poll_for_amendments`.

    `baseline_reference` is the override CELEX, the instrument's
    `effective_date` ISO string, or the literal `"unknown"` when no baseline
    is resolvable.
    """

    regulatory_instrument_id: str
    instrument_type: str
    baseline_reference: str
    detected_consolidated_celex: str
    detected_consolidation_date: date
    reason: Literal["newer_consolidation", "baseline_unknown"]


@dataclass(frozen=True, slots=True)
class PollReport:
    """The complete outcome of one `poll_for_amendments` run.

    `findings` holds only the `amendment_detected` instruments. `failed_ids`
    are per-instrument CELLAR query failures (AC-003); `unconfigured_ids`
    are nodes with no `celex` and no override — a seed gap, not a transient
    failure.
    """

    findings: tuple[AmendmentFinding, ...]
    polled_count: int
    failed_ids: tuple[str, ...]
    unconfigured_ids: tuple[str, ...]


PIPELINE_STAGES: tuple[str, ...] = ("ingestion", "extraction", "derivation", "merge")
"""The re-ingest pipeline stage names, in run order (the strings
`api.ingestion_orchestration._execute_catalog_stages` uses)."""

LINKED = "linked"
"""The `ReingestProgress` marker value meaning every stage finished and the
succession is being (or has been) written. Not a pipeline stage."""


@dataclass(frozen=True, slots=True)
class ReingestionOutcome:
    """The outcome of one `trigger_reingestion` call.

    `state` is the classification the call acted on (`fresh`, `resume`, `repair`,
    `finalize` or `already_processed`; see `trigger.classify_reingestion`). `outcome` is
    `superseded` when the succession is now complete (written by this call), else
    `already_processed`. `run_id` is the pipeline run's id when at least one stage ran in this
    call and `None` otherwise (a link-only or `already_processed` call). `stage_summaries` are
    the stages this call ran, in order (empty when none ran); the audit completion row takes
    its Obligation / Capability counts from the `merge` entry.
    """

    prior_regulatory_instrument_id: str
    new_regulatory_instrument_id: str
    run_id: str | None
    outcome: Literal["superseded", "already_processed"]
    state: Literal["fresh", "resume", "repair", "already_processed", "finalize"] = "fresh"
    stage_summaries: tuple[StageSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class StageSummary:
    """One completed pipeline stage and the small integer summary it produced."""

    stage: str
    summary: dict[str, int]


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    """The stages one `PipelineRunner` call ran, in run order."""

    stages: tuple[StageSummary, ...]


class PipelineRunner(Protocol):
    """Runs a subset of the re-ingest stages (built by the api layer, injected here).

    `change_monitor` must not import `ps_service.api`, so the shared stage
    sequence is handed to `trigger_reingestion` as this callable.
    `on_stage_complete(stage)` is invoked after each stage returns, never for a
    stage that raised.
    """

    def __call__(
        self, *, stages: tuple[str, ...], run_id: str, on_stage_complete: Callable[[str], None]
    ) -> PipelineRunResult:
        """Run `stages` in order and return their summaries."""
        ...
