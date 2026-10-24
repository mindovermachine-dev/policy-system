"""Run the real split executor over a `CleanupRig`.

The approval is created by the real service from the rig's graphs (so it carries the real state
digest and display summary) and marked signed; the executor then runs over the rig's dependencies.
Only the doubles are fakes.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import TYPE_CHECKING

from graph_cleanup._fakes import FakeApprovalStore
from graph_cleanup._gateway_fakes import SPLIT_X
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from ps_service.graph_cleanup.executors import execute_capability_split
from ps_service.graph_cleanup.service import create_capability_split_approval

if TYPE_CHECKING:
    from graph_cleanup._fakes import RecordedAudit
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.passkey_signing.models import PendingApprovalRow

SPLIT_ACTION = "capability.split"


def signed_split_approval(
    rig: CleanupRig, *, acknowledged: bool = True
) -> tuple[FakeApprovalStore, PendingApprovalRow]:
    """A signed `split-capability` approval of `SPLIT_X`, bound to the rig's current state."""
    store = FakeApprovalStore()
    approval = create_capability_split_approval(
        rig.read,
        rig.baseline_graphs(),
        x_id=SPLIT_X,
        acknowledge_governance_change=acknowledged,
        actor=OFFICER,
        base_url="https://ps.example.com",
        store=store,
    )
    assert store.mark_signed(approval.pending_approval_id)
    row = store.get_by_id(approval.pending_approval_id)
    assert row is not None
    return store, row


def split_row(rig: CleanupRig) -> PendingApprovalRow:
    """The signed approval alone (see `signed_split_approval`)."""
    return signed_split_approval(rig)[1]


def with_args(row: PendingApprovalRow, **changes: object) -> PendingApprovalRow:
    """`row` with some signed arguments changed (an approval the flow itself never creates)."""
    return replace(row, normalized_args={**row.normalized_args, **changes})


def run_capability_split(
    rig: CleanupRig,
    *,
    row: PendingApprovalRow,
    dependencies: GraphCleanupDependencies | None = None,
) -> dict[str, object]:
    """Execute the signed split over the rig (its role store grants `OFFICER` by default)."""
    return execute_capability_split(row, CONFIG, dependencies or rig.dependencies(roles()))


type GraphState = tuple[
    dict[tuple[str, str], dict[str, object]],
    dict[tuple[str, str, str, str, str, str], dict[str, object]],
]


def graph_state(rig: CleanupRig) -> GraphState:
    """Every node and edge of the single-tenant graph, with their properties."""
    return copy.deepcopy(rig.graph.nodes), copy.deepcopy(rig.graph.edges)


def split_rows(rig: CleanupRig) -> list[RecordedAudit]:
    """The `capability.split` rows the rig's audit store recorded, in order."""
    return [r for r in rig.audit.rows if r.action == SPLIT_ACTION]


def only_failed_details(rig: CleanupRig, row: PendingApprovalRow) -> dict[str, object]:
    """The details of the single `failed` row a refusal before the write records."""
    [recorded] = split_rows(rig)
    assert (recorded.resource_type, recorded.resource_id) == ("capability", SPLIT_X)
    assert recorded.outcome == "failed"
    assert (recorded.actor_subject, recorded.actor_issuer) == OFFICER
    assert recorded.details["approval_id"] == row.id
    return dict(recorded.details)
