"""Typed `details` for the `instrument.restore` audit action (issue #195).

Registered with `ps_service.audit` at import time (the idiom of `graph_cleanup.audit_actions`);
`api.restore_orchestration` imports this module so registration precedes any record.

One row shape serves the catalog restore (MCP `restore_instrument`,
`POST /restorations/from-catalog`) and the upload restore (`POST /restorations`):
`resource_id` is the canonical instrument id, the details carry the lifecycle `status`
(`started` -> `succeeded | failed`), the `source` (`catalog | upload`) and, on failure, an
enumerated `reason_code`. The model has no field able to hold the curated-source URL, a
path, an exception message or a stack trace (AC-BI-009/010).
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type

__all__ = [
    "INSTRUMENT_RESOURCE_TYPE",
    "INSTRUMENT_RESTORE_ACTION",
    "InstrumentRestoreDetails",
    "RestoreFailureReason",
    "classify_restore_failure_reason",
]

INSTRUMENT_RESTORE_ACTION = "instrument.restore"
INSTRUMENT_RESOURCE_TYPE = "instrument"

type RestoreFailureReason = Literal[
    "artifact_rejected",
    "config_incomplete",
    "content_rejected",
    "concurrent_restore",
    "restore_failed",
    "graph_unavailable",
    "unexpected_error",
]


class InstrumentRestoreDetails(AuditDetails):
    """`instrument.restore`: `applied`/`started` before the restore; a terminal row after it."""

    instrument_id: str
    status: Literal["started", "succeeded", "failed"]
    source: Literal["catalog", "upload"]
    reason_code: RestoreFailureReason | None = None


_STAGE_REASONS: dict[str, RestoreFailureReason] = {
    "configuration": "config_incomplete",
    "content_validation": "content_rejected",
    "concurrency": "concurrent_restore",
}


def classify_restore_failure_reason(exc: Exception) -> RestoreFailureReason:
    """Map a failed restore to the enumerated `reason_code` (never the message or traceback).

    Matched by exception CLASS NAME (and, for a stage failure, its `stage` attribute) so this
    module never imports `ps_service.api` or `ps_service.mcp_interface` (the same naming-by-class
    precedent as `restore_orchestration._classify_restore_failure`).
    """
    name = type(exc).__name__
    if name == "RestoreArtifactRejectedError":
        return "artifact_rejected"
    if name == "McpGraphUnavailableError":
        return "graph_unavailable"
    if name == "RestoreStageFailedError":
        return _STAGE_REASONS.get(str(getattr(exc, "stage", "")), "restore_failed")
    return "unexpected_error"


register_audit_action(INSTRUMENT_RESTORE_ACTION, InstrumentRestoreDetails)
register_audit_resource_type(INSTRUMENT_RESOURCE_TYPE)
