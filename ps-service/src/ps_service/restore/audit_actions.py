"""Typed `details` and failure classification for the `instrument.restore` audit action.

Registers the action and the `instrument` resource type with `ps_service.audit` at import
time; `api.restore_orchestration` imports this module so registration precedes any record.

One row shape serves the catalog restore (MCP `restore_instrument`,
`POST /restorations/from-catalog`) and the upload restore (`POST /restorations`):
`resource_id` is the canonical instrument id, the details carry the lifecycle `status`
(`started` -> `succeeded | failed | rejected | committed_apply_pending`), the `source`
(`catalog | upload`) and, on failure, an enumerated `reason_code`. The model has no field
able to hold the curated-source URL, a path, an exception message or a stack trace.
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
    """Audit `details` of one `instrument.restore` row.

    The opening row (`status="started"`) is written before the restore runs; one terminal row
    follows it. Holds no URL, path, exception message or stack trace.

    Attributes:
        instrument_id: Canonical id of the instrument being restored (non-empty).
        status: Lifecycle state of the restore at this row.
        source: Where the artifact came from: the curated catalog or a direct upload.
        reason_code: Enumerated cause of a `failed` or `rejected` row;
            `None` otherwise.
    """

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
    """The class name of the failure `exc` was raised from, or `""` when it has no cause."""
    cause = exc.__cause__
    return type(cause).__name__ if cause is not None else ""


def classify_restore_failure_reason(exc: Exception) -> RestoreFailureReason:
    """Map a failed restore to the enumerated `reason_code`, never the message or traceback.

    Matching is by exception class name (and, for a stage failure, its `stage` attribute and
    the class of the failure it was raised from), so this module imports neither `ps_service.api`
    nor `ps_service.mcp_interface`.

    Args:
        exc: The exception that ended the restore.

    Returns:
        `artifact_rejected` for a rejected artifact; `graph_unavailable` for an unavailable
        graph or write path; for a stage failure, `group_rejected`, `graph_unavailable`, or the
        reason of its `stage` (`config_incomplete`, `content_rejected`, `concurrent_restore`),
        else `restore_failed`; `unexpected_error` for anything else.
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
    """How a restore's terminal audit row is written.

    Attributes:
        outcome: The audit outcome recorded on the row.
        status: The `status` placed in the row's details.
        reason_code: The enumerated cause placed in the row's details; `None` when the restore
            is committed to the graph log.
    """

    outcome: Literal["applied", "rejected", "failed"]
    status: Literal["succeeded", "failed", "rejected", "committed_apply_pending"]
    reason_code: RestoreFailureReason | None = None


def classify_restore_terminal_row(exc: Exception) -> RestoreTerminalRow:
    """Map a restore that raised to the terminal audit row that records it.

    A group the gateway refused is `rejected` (`group_rejected`); one committed to the graph log
    but not yet applied is `applied` with status `committed_apply_pending` (the data is in the
    log and is applied on recovery); every other failure is `failed` with its enumerated reason.

    Args:
        exc: The exception that ended the restore.

    Returns:
        The outcome, status and reason code to write on the terminal audit row.
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
