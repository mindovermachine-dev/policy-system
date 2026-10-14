"""Assemble an `unmerge` plan: locate the merge in the audit trail, read the graph, plan.

The one place that joins the audit locator, the graph reader and the pure planner. The
preview/approval service and the post-signature executor both call it, so both judge the
same state. Read-only: it never writes to the graph or the audit trail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_cleanup.audit_actions import CapabilityMergeDetails
from ps_service.graph_cleanup.graph_reader import (
    read_capability_unmerge_state,
    read_obligation_unmerge_state,
)
from ps_service.graph_cleanup.unmerge_locator import locate_effective_merge
from ps_service.graph_cleanup.unmerge_planner import (
    derive_capability_unmerge_inputs,
    derive_obligation_unmerge_inputs,
    plan_capability_unmerge,
    plan_obligation_unmerge,
)

if TYPE_CHECKING:
    from ps_service.audit.store import AuditStore
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_cleanup.models import CapabilityUnmergePlan, ObligationUnmergePlan

__all__ = ["UnmergePlan", "plan_unmerge"]

type UnmergePlan = CapabilityUnmergePlan | ObligationUnmergePlan


def plan_unmerge(graph: GraphHandle, audit_store: AuditStore, *, merged_id: str) -> UnmergePlan:
    """Plan reversing the newest effective merge of `merged_id`, with no writes.

    Locates the merge in the audit trail, reads the live graph state it touched, and plans the
    reversal against that state, so a plan exists only when the merge can still be undone.

    Args:
        graph: Handle used to read the live graph; only read queries are issued.
        audit_store: Audit trail searched for the merge row to reverse.
        merged_id: Id of the merged (tombstoned) Capability or Obligation to restore; must
            match the `resource_id` of a recorded merge.

    Returns:
        A `CapabilityUnmergePlan` when the located merge was a capability merge, otherwise an
        `ObligationUnmergePlan`. Each carries the preview, the before/after snapshots and the
        state digest. A plan is never empty: a merge that cannot be reversed raises instead.

    Raises:
        GraphCleanupValidationError: no merge to reverse, an unreadable audit record, or a
            conflict between the merge snapshot and the live graph, each with an explanation.
        GraphCleanupPersistenceError: the graph database could not be read.
        AuditPostgresUnavailableError: the audit trail could not be read.
    """
    located = locate_effective_merge(audit_store, merged_id=merged_id)
    if isinstance(located.details, CapabilityMergeDetails):
        capability_inputs = derive_capability_unmerge_inputs(located.details)
        capability_state = read_capability_unmerge_state(graph, inputs=capability_inputs)
        return plan_capability_unmerge(capability_inputs, capability_state)
    obligation_inputs = derive_obligation_unmerge_inputs(located.details)
    obligation_state = read_obligation_unmerge_state(graph, inputs=obligation_inputs)
    return plan_obligation_unmerge(obligation_inputs, obligation_state)
