"""Typed `details` for the graph-cleanup audit actions (issue #190).

Registers each model with `ps_service.audit.models.register_audit_action` at import time
(the idiom `ps_service.authz.audit_actions` and `ps_service.policy_lifecycle.audit_actions`
use). The emitting module imports this one, so registration precedes any record.

`capability.merge` (AC-BI-021): both ids, policy case, acknowledgment, approval id and a
before/after `GraphSnapshot` sufficient to reverse the merge. `obligation.merge` carries the
same audit contract minus the policy case and acknowledgment, which do not apply to an
Obligation; its `before` snapshot holds the deleted node in full with all of its edges.
An `outcome='failed'` row uses the same model with `before`/`after` optional and `reason_code`
set. An `applied` row followed by a `failed` row for the same `approval_id` means no edit
occurred.
"""

from __future__ import annotations

from typing import Literal

from ps_service.audit.models import (
    AuditDetails,
    register_audit_action,
    register_audit_resource_type,
)
from ps_service.graph_cleanup.models import (  # noqa: TC001 -- pydantic resolves the field annotations at runtime
    EdgeRecord,
    GraphSnapshot,
    MergeCase,
)

__all__ = [
    "CAPABILITY_MERGE_ACTION",
    "CAPABILITY_RELEASE_GOVERNANCE_ACTION",
    "CAPABILITY_UNMERGE_ACTION",
    "OBLIGATION_MERGE_ACTION",
    "OBLIGATION_UNMERGE_ACTION",
    "CapabilityMergeDetails",
    "CapabilityReleaseGovernanceDetails",
    "CapabilityUnmergeDetails",
    "ObligationMergeDetails",
    "ObligationUnmergeDetails",
]

CAPABILITY_MERGE_ACTION = "capability.merge"
OBLIGATION_MERGE_ACTION = "obligation.merge"
CAPABILITY_RELEASE_GOVERNANCE_ACTION = "capability.release_governance"
CAPABILITY_UNMERGE_ACTION = "capability.unmerge"
OBLIGATION_UNMERGE_ACTION = "obligation.unmerge"


class CapabilityMergeDetails(AuditDetails):
    """`capability.merge`: one Compliance Officer capability merge (tombstone).

    `outcome='applied'`: `reason_code` is absent and `before`/`after` are present.
    `outcome='failed'`: `reason_code` names why the edit did not happen (or, for
    `interrupted_no_effect`, that the reconciler found no effect of an approved merge).
    """

    survivor_id: str
    absorbed_id: str
    policy_case: MergeCase
    acknowledged: bool
    approval_id: str
    before: GraphSnapshot | None = None
    after: GraphSnapshot | None = None
    reason_code: (
        Literal["graph_guard_missed", "graph_write_failed", "interrupted_no_effect"] | None
    ) = None
    # Case 2 (M3): the governing policy's status at execution and the ids of every Capability
    # it governed before and after the merge. Absent for case 1 and for an orphan `failed` row.
    policy_id: str | None = None
    policy_status: str | None = None
    governed_set_before: tuple[str, ...] | None = None
    governed_set_after: tuple[str, ...] | None = None


register_audit_action(CAPABILITY_MERGE_ACTION, CapabilityMergeDetails)
register_audit_resource_type("capability")


class ObligationMergeDetails(AuditDetails):
    """`obligation.merge`: one Compliance Officer obligation merge (delete with snapshot).

    `outcome='applied'`: `reason_code` is absent and `before`/`after` are present;
    `before` holds the absorbed Obligation in full and every edge it had.
    `outcome='failed'`: `reason_code` names why the edit did not happen.
    """

    survivor_id: str
    absorbed_id: str
    role_id: str
    approval_id: str
    before: GraphSnapshot | None = None
    after: GraphSnapshot | None = None
    reason_code: (
        Literal["graph_guard_missed", "graph_write_failed", "interrupted_no_effect"] | None
    ) = None


register_audit_action(OBLIGATION_MERGE_ACTION, ObligationMergeDetails)
register_audit_resource_type("obligation")


class CapabilityReleaseGovernanceDetails(AuditDetails):
    """`capability.release_governance`: one Compliance Officer release of a draft policy edge.

    `outcome='applied'`: `before` holds the `GOVERNED_BY` edge, `after` does not, and
    `governed_set_before/after` list every Capability the draft policy governed. `outcome='failed'`:
    `reason_code` names why the edit did not happen and the snapshots are optional.
    """

    capability_id: str
    policy_id: str
    policy_status: str
    approval_id: str
    before: GraphSnapshot | None = None
    after: GraphSnapshot | None = None
    governed_set_before: tuple[str, ...] | None = None
    governed_set_after: tuple[str, ...] | None = None
    reason_code: (
        Literal["graph_guard_missed", "graph_write_failed", "interrupted_no_effect"] | None
    ) = None


register_audit_action(CAPABILITY_RELEASE_GOVERNANCE_ACTION, CapabilityReleaseGovernanceDetails)


class CapabilityUnmergeDetails(AuditDetails):
    """`capability.unmerge`: one Compliance Officer reversal of a capability merge.

    `outcome='applied'`: `before` is the tombstone and survivor state, `after` the restored
    state; `restored_edges` are exactly the edges put back on the absorbed node and
    `survivor_added_edges` those the survivor gained since the merge, which stay in place.
    `reverses_approval_id` is the approval of the merge being reversed and `policy_case` that
    merge's case. `outcome='failed'`: `reason_code` names why the edit did not happen.
    """

    survivor_id: str
    absorbed_id: str
    approval_id: str
    reverses_approval_id: str
    policy_case: MergeCase | None = None
    before: GraphSnapshot | None = None
    after: GraphSnapshot | None = None
    restored_edges: tuple[EdgeRecord, ...] | None = None
    survivor_added_edges: tuple[EdgeRecord, ...] | None = None
    reason_code: (
        Literal["graph_guard_missed", "graph_write_failed", "interrupted_no_effect"] | None
    ) = None


register_audit_action(CAPABILITY_UNMERGE_ACTION, CapabilityUnmergeDetails)


class ObligationUnmergeDetails(AuditDetails):
    """`obligation.unmerge`: one Compliance Officer reversal of an obligation merge.

    `outcome='applied'`: `before` is the marker and survivor state, `after` the recreated
    Obligation with its edges; `restored_edges` are exactly the edges put back on it.
    `survivor_added_edges` are edges the survivor gained since the merge and
    `survivor_edges_possibly_from_merge` those it may have received from the absorbed node:
    both stay on the survivor. `reverses_approval_id` is the approval of the merge being
    reversed. `outcome='failed'`: `reason_code` names why the edit did not happen.
    """

    survivor_id: str
    absorbed_id: str
    role_id: str
    approval_id: str
    reverses_approval_id: str
    before: GraphSnapshot | None = None
    after: GraphSnapshot | None = None
    restored_edges: tuple[EdgeRecord, ...] | None = None
    survivor_added_edges: tuple[EdgeRecord, ...] | None = None
    survivor_edges_possibly_from_merge: tuple[EdgeRecord, ...] | None = None
    reason_code: (
        Literal["graph_guard_missed", "graph_write_failed", "interrupted_no_effect"] | None
    ) = None


register_audit_action(OBLIGATION_UNMERGE_ACTION, ObligationUnmergeDetails)
