"""Typed `details` models, names and pure entry builders for the `ingestion_run.*` audit actions.

Mirrors `ps_service.runtime_config.audit_actions`: each model is registered with
`ps_service.audit` at import time through its public registry only (the package `__init__`
imports this module for that side effect).

`audit_events.outcome` is constrained to `applied|rejected|failed`, so AC-BI-016's
"outcome=started" is stored as `outcome="applied"` (the *submission* was applied) plus
`details.status="started"`. A completion is `applied` for a succeeded run and `failed` for a
failed one. The builders are pure and shared by the real store and the test fake, so the two
can never drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self

from pydantic import NonNegativeInt, model_validator

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type
from ps_service.ingestion_runs.errors import IngestionRunInvalidCompletionError

if TYPE_CHECKING:
    from collections.abc import Mapping

type IngestionTrigger = Literal["sync_ingest", "async_ingest", "amendment_check"]
"""Which entry point started the run: sync ingest (MCP/REST), `start_ingestion`, or a sweep."""

type IngestionReasonCode = Literal[
    "config_incomplete",
    "graph_unavailable",
    "celex_not_found",
    "short_name_collision",
    "pipeline_stage_failed",
    "unsupported_instrument_type",
    "inconsistent_graph_state",
    "interrupted",
    "unexpected_error",
]
"""Closed set of failure causes (issue #195, AC-BI-010), derived from the exception TYPE only."""


class IngestionRunSubmitDetails(AuditDetails):
    """`ingestion_run.submit` -- always `outcome='applied'`, `status='started'`."""

    celex: str
    short_name: str
    status: Literal["started"]
    trigger: IngestionTrigger


class IngestionRunCompleteDetails(AuditDetails):
    """`ingestion_run.complete` -- `applied` when succeeded, `failed` when failed.

    Carries no free-text error (AC-BI-010): a failure is an enumerated `reason_code`. The three
    counts are facts the Company Merge stage computed (zero when the run produced none).
    """

    status: Literal["succeeded", "failed"]
    celex: str
    trigger: IngestionTrigger
    regulatory_instrument_id: str | None = None
    outcome: Literal["fresh", "already_ingested"] | None = None
    new_obligations: NonNegativeInt
    new_capabilities: NonNegativeInt
    matched_capabilities: NonNegativeInt
    reason_code: IngestionReasonCode | None = None

    @model_validator(mode="after")
    def _reason_code_iff_failed(self) -> Self:
        if (self.status == "failed") != (self.reason_code is not None):
            msg = "reason_code is required exactly when status is 'failed'"
            raise ValueError(msg)
        return self


INGESTION_RUN_SUBMIT_ACTION = "ingestion_run.submit"
INGESTION_RUN_COMPLETE_ACTION = "ingestion_run.complete"
INGESTION_RUN_RESOURCE_TYPE = "ingestion_run"
INGESTION_RUN_RECONCILER_ACTOR = "system:ingestion-run-reconciler"
"""Subject and issuer of the audit actor for a run reconciled by a status poll (OQ-7)."""

register_audit_action(INGESTION_RUN_SUBMIT_ACTION, IngestionRunSubmitDetails)
register_audit_action(INGESTION_RUN_COMPLETE_ACTION, IngestionRunCompleteDetails)
register_audit_resource_type(INGESTION_RUN_RESOURCE_TYPE)


@dataclass(frozen=True, slots=True)
class IngestionRunAuditEntry:
    """One audit row's `action`, `outcome` and `details`, before the actor and run id are bound."""

    action: str
    outcome: Literal["applied", "failed"]
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class IngestionCounts:
    """Net-new Obligations / Capabilities and matched Capabilities of one run (AC-BI-005)."""

    new_obligations: int
    new_capabilities: int
    matched_capabilities: int

    @classmethod
    def from_result(cls, result: dict[str, object] | None) -> IngestionCounts:
        """Read the counts from the `merge` stage summary of an accepted-response dict.

        Zero when there is no result, no merge stage (an already-ingested catalog request) or a
        non-integer value.
        """
        return cls.from_merge_summary(_merge_summary_of(result))

    @classmethod
    def from_merge_summary(cls, summary: Mapping[str, object]) -> IngestionCounts:
        """Read the counts from one merge stage `summary` mapping (zero for anything missing)."""
        return cls(
            new_obligations=_count(summary, "new_obligations"),
            new_capabilities=_count(summary, "new_capabilities"),
            matched_capabilities=_count(summary, "matched_capabilities"),
        )


def _merge_summary_of(result: dict[str, object] | None) -> Mapping[str, object]:
    stages = (result or {}).get("stages")
    if not isinstance(stages, list):
        return {}
    for stage in stages:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(stage, dict) and stage.get("stage") == "merge":  # pyright: ignore[reportUnknownMemberType]
            summary = stage.get("summary")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            return summary if isinstance(summary, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    return {}


def _count(summary: Mapping[str, object], key: str) -> int:
    value = summary.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def submission_audit_entry(
    *, celex: str, short_name: str, trigger: IngestionTrigger
) -> IngestionRunAuditEntry:
    """The entry recording that a run was accepted and started (AC-BI-004)."""
    return IngestionRunAuditEntry(
        action=INGESTION_RUN_SUBMIT_ACTION,
        outcome="applied",
        details={
            "celex": celex,
            "short_name": short_name,
            "status": "started",
            "trigger": trigger,
        },
    )


def require_reason_code_iff_failed(
    *, status: Literal["succeeded", "failed"], reason_code: IngestionReasonCode | None
) -> None:
    """Fail fast when a completion's `reason_code` disagrees with its status (AC-BI-010).

    The same rule `IngestionRunCompleteDetails` enforces at audit time, checked at the store
    boundary so a bad call is rejected before any write rather than deep inside a transaction.

    Raises:
        IngestionRunInvalidCompletionError: `reason_code` is missing on a failure, or present
            on a success.
    """
    if (status == "failed") != (reason_code is not None):
        raise IngestionRunInvalidCompletionError


def completion_audit_entry(
    *,
    status: Literal["succeeded", "failed"],
    celex: str,
    trigger: IngestionTrigger,
    result: dict[str, object] | None,
    reason_code: IngestionReasonCode | None,
) -> IngestionRunAuditEntry:
    """The entry recording a run's real terminal outcome (AC-BI-005, AC-BI-010).

    Never carries error text: a failed run is described by `reason_code` alone.
    """
    counts = IngestionCounts.from_result(result)
    details: dict[str, object] = {
        "status": status,
        "celex": celex,
        "trigger": trigger,
        "new_obligations": counts.new_obligations,
        "new_capabilities": counts.new_capabilities,
        "matched_capabilities": counts.matched_capabilities,
    }
    if status == "failed":
        details["reason_code"] = reason_code
        return IngestionRunAuditEntry(
            action=INGESTION_RUN_COMPLETE_ACTION, outcome="failed", details=details
        )
    for key in ("regulatory_instrument_id", "outcome"):
        value = (result or {}).get(key)
        if isinstance(value, str):
            details[key] = value
    return IngestionRunAuditEntry(
        action=INGESTION_RUN_COMPLETE_ACTION, outcome="applied", details=details
    )


__all__ = [
    "INGESTION_RUN_COMPLETE_ACTION",
    "INGESTION_RUN_RECONCILER_ACTOR",
    "INGESTION_RUN_RESOURCE_TYPE",
    "INGESTION_RUN_SUBMIT_ACTION",
    "IngestionCounts",
    "IngestionReasonCode",
    "IngestionRunAuditEntry",
    "IngestionRunCompleteDetails",
    "IngestionRunSubmitDetails",
    "IngestionTrigger",
    "completion_audit_entry",
    "require_reason_code_iff_failed",
    "submission_audit_entry",
]
