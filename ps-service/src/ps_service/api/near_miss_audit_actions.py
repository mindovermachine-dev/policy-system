"""Typed `details` for the `near_miss.resolve` audit action.

Importing this module registers the action and its resource type with `ps_service.audit`, so
`near_miss_review_orchestration` imports it to guarantee registration precedes any record.

One row shape serves both decisions and all three transports (MCP, REST and the
passkey-approved merge): `resource_id` is the review id, and the details carry both entity ids,
the decision and, for a merge, the `approval_id` (an id only, never the approval code or any
credential). A `failed` follow-up row adds an enumerated `reason_code`; no free-text error or
stack trace is ever recorded.
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

# Enumerated failure reasons; a closed set so no free text reaches the audit trail.
type NearMissResolveReason = Literal[
    "review_not_found",
    "review_stale",
    "graph_write_failed",
    "graph_unavailable",
    "unexpected_error",
]


class NearMissResolveDetails(AuditDetails):
    """Audit `details` payload for a `near_miss.resolve` row.

    Written with outcome `applied` before the resolution takes effect, and again with outcome
    `failed` (carrying `reason_code`) if the effect then fails.

    Attributes:
        review_id: Identifier of the near-miss pending review being resolved.
        kind: Kind of entity pair under review, `"Capability"` or `"Policy"`.
        incoming_id: Identifier of the newly derived entity of the pair.
        existing_id: Identifier of the entity already in the graph that it resembles.
        decision: `"keep_separate"` to retain both entities, `"merge"` to merge them.
        approval_id: Identifier of the passkey approval authorising a merge; `None` for
            `keep_separate`. Never the approval code.
        reason_code: Enumerated failure reason; `None` on the `applied` row.
    """

    review_id: str
    kind: Literal["Capability", "Policy"]
    incoming_id: str
    existing_id: str
    decision: Literal["keep_separate", "merge"]
    approval_id: str | None = None
    reason_code: NearMissResolveReason | None = None


register_audit_action(NEAR_MISS_RESOLVE_ACTION, NearMissResolveDetails)
register_audit_resource_type(NEAR_MISS_REVIEW_RESOURCE_TYPE)
