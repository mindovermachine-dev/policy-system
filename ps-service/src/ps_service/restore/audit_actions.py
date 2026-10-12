"""Typed `details` for the `instrument.restore` audit action (issue #195).

Registered with `ps_service.audit` at import time (the idiom of `graph_cleanup.audit_actions`);
`api.restore_orchestration` imports this module so registration precedes any record.

One row shape serves the catalog restore (MCP `restore_instrument`,
`POST /restorations/from-catalog`) and the upload restore (`POST /restorations`):
`resource_id` is the canonical instrument id, the details carry the lifecycle `status`
(`started` -> `succeeded | failed | rejected | committed_apply_pending`), the `source`
(`catalog | upload`) and, on failure, an enumerated `reason_code`. The model has no field
able to hold the curated-source URL, a path, an exception message or a stack trace
(AC-BI-009/010).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type

__all__ = [
    "INSTRUMENT_RESOURCE_TYPE",
    "INSTRUMENT_RESTORE_ACTION",
    "InstrumentRestoreDetails",
    "RestoreFailureReason",
    "RestoreTerminalRow",
    "classify_restore_failure_reason",
    "classify_restore_terminal_row",
]

INSTRUMENT_RESTORE_ACTION = "instrument.restore"
INSTRUMENT_RESOURCE_TYPE = "instrument"

type RestoreFailureReason = Literal[
    "artifact_rejected",
    "config_incomplete",
    "content_rejected",
    "concurrent_restore",
    "group_rejected",
    "restore_failed",
    "graph_unavailable",
    "unexpected_error",
]


class InstrumentRestoreDetails(AuditDetails):
    """`instrument.restore`: `applied`/`started` before the restore; a terminal row after it."""

    instrument_id: str
    status: Literal["started", "succeeded", "failed", "rejected", "committed_apply_pending"]
    source: Literal["catalog", "upload"]
    reason_code: RestoreFailureReason | None = None


_STAGE_REASONS: dict[str, RestoreFailureReason] = {
    "configuration": "config_incomplete",
    "content_validation": "content_rejected",
    "concurrency": "concurrent_restore",
}


def _cause_name(exc: Exception) -> str:
    """The class name of the restore failure a `RestoreStageFailedError` was raised from."""
    cause = exc.__cause__
    return type(cause).__name__ if cause is not None else ""


def classify_restore_failure_reason(exc: Exception) -> RestoreFailureReason:
    """Map a failed restore to the enumerated `reason_code` (never the message or traceback).

    Matched by exception CLASS NAME (and, for a stage failure, its `stage` attribute and the
    class of the failure it was raised from) so this module never imports `ps_service.api` or
    `ps_service.mcp_interface` (the same naming-by-class precedent as
    `restore_orchestration._classify_restore_failure`).
    """
    name = type(exc).__name__
    if name == "RestoreArtifactRejectedError":
        return "artifact_rejected"
    if name in {"McpGraphUnavailableError", "GraphWriteUnavailableError"}:
        return "graph_unavailable"
    if name == "RestoreStageFailedError":
        cause = _cause_name(exc)
        if cause == "RestoreGroupRejectedError":
            return "group_rejected"
        if cause == "RestoreGraphWriteFailedError":
            return "graph_unavailable"
        return _STAGE_REASONS.get(str(getattr(exc, "stage", "")), "restore_failed")
    return "unexpected_error"


@dataclass(frozen=True, slots=True)
class RestoreTerminalRow:
    """How a restore's terminal audit row is written: its outcome, status and reason code."""

    outcome: Literal["applied", "rejected", "failed"]
    status: Literal["succeeded", "failed", "rejected", "committed_apply_pending"]
    reason_code: RestoreFailureReason | None = None


def classify_restore_terminal_row(exc: Exception) -> RestoreTerminalRow:
    """Map a restore that raised to its terminal audit row (issue #214, D-5).

    A group the gateway refused is `rejected` (`group_rejected`); one committed to the graph log
    but not yet applied is `applied` with status `committed_apply_pending` (the data is in the
    log and is applied on recovery); every other failure is `failed` with its enumerated reason.
    """
    if type(exc).__name__ == "RestoreStageFailedError":
        cause = _cause_name(exc)
        if cause == "RestoreGroupRejectedError":
            return RestoreTerminalRow("rejected", "rejected", "group_rejected")
        if cause == "RestoreApplyPendingError":
            return RestoreTerminalRow("applied", "committed_apply_pending")
    return RestoreTerminalRow("failed", "failed", classify_restore_failure_reason(exc))


register_audit_action(INSTRUMENT_RESTORE_ACTION, InstrumentRestoreDetails)
register_audit_resource_type(INSTRUMENT_RESOURCE_TYPE)
