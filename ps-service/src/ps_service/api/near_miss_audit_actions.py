"""Typed `details` for the `near_miss.resolve` audit action (issue #195).

Registered with `ps_service.audit` at import time (the idiom of `graph_cleanup.audit_actions`);
`near_miss_review_orchestration` imports this module so registration precedes any record.

One row shape serves both decisions and all three transports (MCP, REST, passkey-approved merge):
`resource_id` is the review id, the details carry both entity ids, the decision and, for a merge,
the `approval_id` (an id only: never the approval code or any credential). A `failed` follow-up
row adds an enumerated `reason_code`; no free-text error or stack trace is ever recorded
(AC-BI-009/010).
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit import AuditDetails, register_audit_action, register_audit_resource_type

__all__ = [
    "NEAR_MISS_RESOLVE_ACTION",
    "NEAR_MISS_REVIEW_RESOURCE_TYPE",
    "NearMissResolveDetails",
    "NearMissResolveReason",
]

NEAR_MISS_RESOLVE_ACTION = "near_miss.resolve"
NEAR_MISS_REVIEW_RESOURCE_TYPE = "near_miss_review"

type NearMissResolveReason = Literal[
    "review_not_found",
    "review_stale",
    "graph_write_failed",
    "graph_unavailable",
    "unexpected_error",
]


class NearMissResolveDetails(AuditDetails):
    """`near_miss.resolve`: `applied` before the effect; `failed` after it fails."""

    review_id: str
    kind: Literal["Capability", "Policy"]
    incoming_id: str
    existing_id: str
    decision: Literal["keep_separate", "merge"]
    approval_id: str | None = None
    reason_code: NearMissResolveReason | None = None


register_audit_action(NEAR_MISS_RESOLVE_ACTION, NearMissResolveDetails)
register_audit_resource_type(NEAR_MISS_REVIEW_RESOURCE_TYPE)
