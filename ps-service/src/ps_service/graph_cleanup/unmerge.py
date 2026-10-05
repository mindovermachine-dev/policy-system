"""Assemble an `unmerge` plan: locate the merge in the audit trail, read the graph, plan (#190).

The one place that joins the audit locator, the reader and the pure planner, shared by the
preview/approval service and the post-signature executor so both judge the same state.
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

    Raises:
        GraphCleanupValidationError: no merge to reverse, an unreadable audit record, or a
            conflict between the merge snapshot and the live graph (AC-BI-020), each with an
            explanation.
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
