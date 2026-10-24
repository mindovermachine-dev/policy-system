"""Assemble an `unmerge-entities` plan: locate the merge in the audit trail, read the graph, plan.

The one place that joins the audit locator, the graph reader and the pure planner. The
preview/approval service and the post-signature executor both call it, so both judge the
same state. Read-only: it never writes to the graph or the audit trail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ps_service.graph_cleanup.errors import GraphCleanupValidationError
from ps_service.graph_cleanup.graph_reader import (
    read_capability_unmerge_state,
    read_obligation_unmerge_state,
    read_role_unmerge_state,
)
from ps_service.graph_cleanup.role_dependency_locator import find_dependent_merge
from ps_service.graph_cleanup.role_unmerge_planner import (
    derive_role_unmerge_inputs,
    plan_role_unmerge,
)
from ps_service.graph_cleanup.unmerge_locator import locate_effective_merge
from ps_service.graph_cleanup.unmerge_planner import (
    derive_capability_unmerge_inputs,
    derive_obligation_unmerge_inputs,
    plan_capability_unmerge,
    plan_obligation_unmerge,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.audit.store import AuditStore
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_cleanup.audit_actions import (
        CapabilityMergeDetails,
        ObligationMergeDetails,
        RoleMergeDetails,
    )
    from ps_service.graph_cleanup.models import (
        CapabilityUnmergePlan,
        ObligationUnmergePlan,
        RoleUnmergePlan,
    )
    from ps_service.graph_cleanup.unmerge_locator import LocatedMerge

__all__ = ["UnmergePlan", "plan_unmerge"]

type UnmergePlan = CapabilityUnmergePlan | ObligationUnmergePlan | RoleUnmergePlan


def _plan_capability(
    graph: GraphHandle, _audit_store: AuditStore, merge: LocatedMerge
) -> UnmergePlan:
    inputs = derive_capability_unmerge_inputs(cast("CapabilityMergeDetails", merge.details))
    return plan_capability_unmerge(inputs, read_capability_unmerge_state(graph, inputs=inputs))


def _plan_obligation(
    graph: GraphHandle, _audit_store: AuditStore, merge: LocatedMerge
) -> UnmergePlan:
    inputs = derive_obligation_unmerge_inputs(cast("ObligationMergeDetails", merge.details))
    return plan_obligation_unmerge(inputs, read_obligation_unmerge_state(graph, inputs=inputs))


def _refuse_if_depended_on(audit_store: AuditStore, merge: LocatedMerge) -> None:
    """Raise when a later merge stands on the Role merge, naming that merge's audit event."""
    details = cast("RoleMergeDetails", merge.details)
    dependent = find_dependent_merge(audit_store, merge, details)
    if dependent is not None:
        message = (
            f"role {details.absorbed_id!r} cannot be unmerged: a later {dependent.action} "
            f"(audit event {dependent.event_id}, approval {dependent.approval_id}) depends on "
            "it; reverse that merge first"
        )
        raise GraphCleanupValidationError(message)


def _plan_role(graph: GraphHandle, audit_store: AuditStore, merge: LocatedMerge) -> UnmergePlan:
    _refuse_if_depended_on(audit_store, merge)
    inputs = derive_role_unmerge_inputs(cast("RoleMergeDetails", merge.details))
    return plan_role_unmerge(inputs, read_role_unmerge_state(graph, inputs=inputs))


# One planner per kind of merge; the located merge's `kind` picks it.
_PLANNERS: dict[str, Callable[[GraphHandle, AuditStore, LocatedMerge], UnmergePlan]] = {
    "capability": _plan_capability,
    "obligation": _plan_obligation,
    "role": _plan_role,
}


def plan_unmerge(graph: GraphHandle, audit_store: AuditStore, *, merged_id: str) -> UnmergePlan:
    """Plan reversing the newest effective merge of `merged_id`, with no writes.

    Locates the merge in the audit trail, reads the live graph state it touched, and plans the
    reversal against that state, so a plan exists only when the merge can still be undone.

    Args:
        graph: Handle used to read the live graph; only read queries are issued.
        audit_store: Audit trail searched for the merge row to reverse.
        merged_id: Id of the merged (deleted, marked) Capability, Obligation or Role to restore;
            must match the `resource_id` of a recorded merge.

    Returns:
        A `CapabilityUnmergePlan`, `ObligationUnmergePlan` or `RoleUnmergePlan`, matching the kind
        of the located merge. Each carries the preview, the before/after snapshots and the
        state digest. A plan is never empty: a merge that cannot be reversed raises instead.

    Raises:
        GraphCleanupValidationError: no merge to reverse, an unreadable audit record, or a
            conflict between the merge snapshot and the live graph, each with an explanation.
        GraphCleanupPersistenceError: the graph database could not be read.
        AuditPostgresUnavailableError: the audit trail could not be read.
    """
    located = locate_effective_merge(audit_store, merged_id=merged_id)
    return _PLANNERS[located.kind](graph, audit_store, located)
