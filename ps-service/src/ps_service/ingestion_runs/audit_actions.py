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
from typing import Literal

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type


class IngestionRunSubmitDetails(AuditDetails):
    """`ingestion_run.submit` -- always `outcome='applied'`, `status='started'`."""

    celex: str
    short_name: str
    status: Literal["started"]


class IngestionRunCompleteDetails(AuditDetails):
    """`ingestion_run.complete` -- `applied` when succeeded, `failed` when failed."""

    status: Literal["succeeded", "failed"]
    regulatory_instrument_id: str | None = None
    outcome: Literal["fresh", "already_ingested"] | None = None
    error: str | None = None


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


def submission_audit_entry(*, celex: str, short_name: str) -> IngestionRunAuditEntry:
    """The entry recording that a run was accepted and started (AC-BI-016)."""
    return IngestionRunAuditEntry(
        action=INGESTION_RUN_SUBMIT_ACTION,
        outcome="applied",
        details={"celex": celex, "short_name": short_name, "status": "started"},
    )


def completion_audit_entry(
    *,
    status: Literal["succeeded", "failed"],
    result: dict[str, object] | None,
    error: str | None,
) -> IngestionRunAuditEntry:
    """The entry recording a run's real terminal outcome (AC-BI-017)."""
    details: dict[str, object] = {"status": status}
    if status == "succeeded":
        for key in ("regulatory_instrument_id", "outcome"):
            value = (result or {}).get(key)
            if isinstance(value, str):
                details[key] = value
        return IngestionRunAuditEntry(
            action=INGESTION_RUN_COMPLETE_ACTION, outcome="applied", details=details
        )
    if error is not None:
        details["error"] = error
    return IngestionRunAuditEntry(
        action=INGESTION_RUN_COMPLETE_ACTION, outcome="failed", details=details
    )


__all__ = [
    "INGESTION_RUN_COMPLETE_ACTION",
    "INGESTION_RUN_RECONCILER_ACTOR",
    "INGESTION_RUN_RESOURCE_TYPE",
    "INGESTION_RUN_SUBMIT_ACTION",
    "IngestionRunAuditEntry",
    "IngestionRunCompleteDetails",
    "IngestionRunSubmitDetails",
    "completion_audit_entry",
    "submission_audit_entry",
]
