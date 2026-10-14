"""`ps_service.change_monitor` core types.

The shapes `poll.py` / `trigger.py` / `graph_reader.py` build and consume.
All are plain frozen dataclasses: they are in-process values passed between
`poll_for_amendments` / `trigger_reingestion` and their callers, never
serialized across a boundary, so they need no Pydantic validation.
`ConsolidatedVersionInfo` lives in `cellar_consolidated.py`, not here.
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

    Attributes:
        regulatory_instrument_id: Graph id of the `RegulatoryInstrument` node.
        celex: The node's base-act CELEX, or `None` when the optional `celex`
            property is absent (the instrument cannot be polled by CELEX).
        instrument_type: `regulation` or `directive`.
        effective_date: ISO date string exactly as Ingestion stored it
            (`date.isoformat()`); empty string when the node has none.
    """

    regulatory_instrument_id: str
    celex: str | None
    instrument_type: str
    effective_date: str


@dataclass(frozen=True, slots=True)
class PriorInstrument:
    """The single active prior `RegulatoryInstrument` a new version supersedes.

    Identified by `succession.find_prior_instrument`: the `status='active'`
    node that is neither the new node nor already superseded into it.

    Attributes:
        id: Graph id of the prior `RegulatoryInstrument`.
        instrument_type: Its instrument type, carried so `trigger_reingestion`
            can reject a `national_transposition` prior without a second read.
    """

    id: str
    instrument_type: str


@dataclass(frozen=True, slots=True)
class AmendmentFinding:
    """One `amendment_detected` result row from `poll_for_amendments`.

    Attributes:
        regulatory_instrument_id: Graph id of the instrument with a newer
            version available.
        instrument_type: `regulation` or `directive`.
        baseline_reference: The override CELEX, the instrument's
            `effective_date` ISO string, or the literal `"unknown"` when no
            baseline is resolvable.
        detected_consolidated_celex: Newest consolidated CELEX found in CELLAR.
        detected_consolidation_date: Date encoded in that CELEX's suffix.
        reason: `newer_consolidation` when CELLAR's date is later than the
            baseline; `baseline_unknown` when no baseline could be resolved.
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

    Attributes:
        findings: Only the instruments with a detected amendment.
        polled_count: Number of tracked instruments examined.
        failed_ids: Instruments whose CELLAR query failed; a transient
            per-instrument failure that does not stop the run.
        unconfigured_ids: Instruments with no `celex` and no override, so
            they cannot be polled; a data gap, not a transient failure.
    """

    findings: tuple[AmendmentFinding, ...]
    polled_count: int
    failed_ids: tuple[str, ...]
    unconfigured_ids: tuple[str, ...]


PIPELINE_STAGES: tuple[str, ...] = ("ingestion", "extraction", "derivation", "merge")
"""The re-ingest pipeline stage names, in run order."""

LINKED = "linked"
"""The `ReingestProgress` marker value meaning every stage finished and the
succession is being (or has been) written. Not a pipeline stage."""


@dataclass(frozen=True, slots=True)
class ReingestionOutcome:
    """The outcome of one `trigger_reingestion` call.

    Attributes:
        prior_regulatory_instrument_id: Graph id of the superseded instrument.
        new_regulatory_instrument_id: Graph id of the new version.
        run_id: The pipeline run's id when at least one stage ran in this call;
            `None` for a link-only or `already_processed` call.
        outcome: `superseded` when this call completed the succession, else
            `already_processed`.
        state: The classification the call acted on (`fresh`, `resume`,
            `repair`, `finalize` or `already_processed`; see
            `trigger.classify_reingestion`).
        stage_summaries: The stages this call ran, in order; empty when none
            ran. The audit completion row takes its Obligation / Capability
            counts from the `merge` entry.
    """

    prior_regulatory_instrument_id: str
    new_regulatory_instrument_id: str
    run_id: str | None
    outcome: Literal["superseded", "already_processed"]
    state: Literal["fresh", "resume", "repair", "already_processed", "finalize"] = "fresh"
    stage_summaries: tuple[StageSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class StageSummary:
    """One completed pipeline stage and the small integer summary it produced.

    Attributes:
        stage: Stage name, one of `PIPELINE_STAGES`.
        summary: Counter name to integer count reported by the stage.
    """

    stage: str
    summary: dict[str, int]


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    """The stages one `PipelineRunner` call ran, in run order.

    Attributes:
        stages: One `StageSummary` per stage that completed, in run order.
    """

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
        """Run `stages` in order and return their summaries.

        Args:
            stages: Stage names to run, a subset of `PIPELINE_STAGES`, in run order.
            run_id: Id of the pipeline run the stages belong to.
            on_stage_complete: Called with the stage name after each stage returns.

        Returns:
            A `PipelineRunResult` holding one summary per stage run.
        """
        ...
