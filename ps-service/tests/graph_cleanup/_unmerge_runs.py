"""Run the real unmerge executor over a `CleanupRig`.

An unmerge is planned from the audit trail (the merge's `applied` row) and the live graph, so a
world here is two things: the aftermath of a merge in the rig's graph and the merge's row in the
rig's audit history. The gateway, equivalence and continuity tests of both unmerge flows start the
same way: a signed approval row bound to the plan's state digest, then the real executor over the
rig's dependencies. Only the doubles are fakes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from graph_cleanup._fakes import (
    ABSORBED,
    OBL_ABSORBED,
    capability_merge_history,
    obligation_merge_history,
)
from graph_cleanup._merge_runs import CONFIG, OFFICER, roles
from ps_service.graph_cleanup.executors import TOOL_UNMERGE, execute_unmerge
from ps_service.graph_cleanup.unmerge import plan_unmerge
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.graph_cleanup.models import (
        CapabilityUnmergePlan,
        ObligationUnmergePlan,
    )

POLICY_TITLE = ("pol_1", "Incident Policy")


def capability_unmerge_world(
    rig: CleanupRig, *, policy: str | None = None, legacy: bool = False
) -> None:
    """Seed the aftermath of a capability merge and put the merge's row in the audit history.

    `policy` is `None` (case 1), `"moved"` (case 2) or `"kept"` (case 3), as the seeder takes it.
    """
    rig.seed_capability_unmerge_world(policy=policy, legacy=legacy)
    rig.audit.history = capability_merge_history(
        absorbed_policy=POLICY_TITLE if policy else None,
        survivor_also_governed=policy == "kept",
    )


def obligation_unmerge_world(rig: CleanupRig, *, legacy: bool = False) -> None:
    """Seed the aftermath of an obligation merge and the merge's row in the audit history."""
    rig.seed_obligation_unmerge_world(legacy=legacy)
    rig.audit.history = obligation_merge_history()


def unmerge_plan(rig: CleanupRig, merged_id: str) -> CapabilityUnmergePlan | ObligationUnmergePlan:
    """The plan the preview computes from the rig's graph and audit history."""
    return plan_unmerge(rig.read, rig.audit, merged_id=merged_id)


def unmerge_row(rig: CleanupRig, merged_id: str, **args: object) -> PendingApprovalRow:
    """A signed `unmerge` approval bound to the rig's current state."""
    plan = unmerge_plan(rig, merged_id)
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-2",
        code_hash=b"h",
        tool_name=TOOL_UNMERGE,
        normalized_args={
            "merged_id": merged_id,
            "merge_approval_id": "merge-approval-1",
            "kind": plan.preview.kind,
            "state_digest": plan.state_digest,
            **args,
        },
        actor_subject=OFFICER[0],
        actor_issuer=OFFICER[1],
        nonce=b"n",
        display_summary={},
        status="signed",
        outcome=None,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )


def run_unmerge(
    rig: CleanupRig,
    merged_id: str,
    *,
    row: PendingApprovalRow | None = None,
    dependencies: GraphCleanupDependencies | None = None,
) -> dict[str, object]:
    """Execute a signed unmerge (`row` when the test armed a read hook first)."""
    return execute_unmerge(
        row or unmerge_row(rig, merged_id), CONFIG, dependencies or rig.dependencies(roles())
    )


def run_capability_unmerge(rig: CleanupRig, **kwargs: object) -> dict[str, object]:
    """Execute a signed capability unmerge of `ABSORBED`."""
    return run_unmerge(rig, ABSORBED, **kwargs)  # pyright: ignore[reportArgumentType]


def run_obligation_unmerge(rig: CleanupRig, **kwargs: object) -> dict[str, object]:
    """Execute a signed obligation unmerge of `OBL_ABSORBED`."""
    return run_unmerge(rig, OBL_ABSORBED, **kwargs)  # pyright: ignore[reportArgumentType]
