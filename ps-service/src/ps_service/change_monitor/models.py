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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import date

    from ps_service.ingestion.models import ReachabilityCount


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


@dataclass(frozen=True, slots=True)
class ReingestionOutcome:
    """The outcome of one `trigger_reingestion` call.

    `run_id` is the re-ingest's `IngestResult.run_id` on the `fresh` path and
    `None` on the `resume` / `already_processed` paths (no `IngestResult` in
    hand). `ingest_counts` is `None` whenever no re-ingest ran.
    """

    prior_regulatory_instrument_id: str
    new_regulatory_instrument_id: str
    run_id: str | None
    outcome: Literal["superseded", "already_processed"]
    ingest_counts: dict[str, ReachabilityCount] | None
