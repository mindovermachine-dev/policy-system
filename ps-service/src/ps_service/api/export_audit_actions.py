"""Typed `details` for the `export.instrument` audit action.

`POST /exports` has no audit context of its own, and Export is the one writer of the embedding
backfill, so each run records one system audit row through the `SystemAuditRecorder` and anchors
the instrument's `{short}_baseline` graph group to that row. That row is the only observable
effect of routing the backfill through the graph gateway.

Importing this module registers the action and its resource type with `ps_service.audit`, so
`export_orchestration` imports it to guarantee registration precedes any record. It lives in
`api/`, not `export/`, so that importing `ps_service.main` does not import `ps_service.export.*`.
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type

__all__ = [
    "EXPORT_INSTRUMENT_ACTION",
    "EXPORT_INSTRUMENT_RESOURCE_TYPE",
    "ExportInstrumentDetails",
]

EXPORT_INSTRUMENT_ACTION = "export.instrument"
EXPORT_INSTRUMENT_RESOURCE_TYPE = "instrument"


class ExportInstrumentDetails(AuditDetails):
    """Audit `details` payload for an `export.instrument` row.

    Written with outcome `applied` before the embedding backfill starts. It names the instrument
    only and carries no content or credentials.

    Attributes:
        instrument_id: Identifier of the instrument being exported, for example `cra-1`.
        status: Always `"started"`; the row marks the start of the backfill, not its result.
    """

    instrument_id: str
    status: Literal["started"]


register_audit_action(EXPORT_INSTRUMENT_ACTION, ExportInstrumentDetails)
register_audit_resource_type(EXPORT_INSTRUMENT_RESOURCE_TYPE)
