"""Typed `details` for the `export.instrument` audit action (issue #213).

`POST /exports` has no audit context of its own and Export is the one writer of the embedding
backfill, so it records one system audit row per run (the internal-seed ingestion precedent,
issue #208) through the `SystemAuditRecorder` and anchors its `{short}_baseline` group to that
row. The row is the only new observable effect of routing the backfill through the gateway.

Registered with `ps_service.audit` at import time (the idiom of `near_miss_audit_actions`);
`export_orchestration` imports this module so registration precedes any record. It lives in
`api/`, not `export/`, to keep `ps_service.main` off `ps_service.export.*` at import (M6).
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
    """`export.instrument`: `applied` before the embedding backfill; names the instrument only."""

    instrument_id: str
    status: Literal["started"]


register_audit_action(EXPORT_INSTRUMENT_ACTION, ExportInstrumentDetails)
register_audit_resource_type(EXPORT_INSTRUMENT_RESOURCE_TYPE)
