"""Run the real merge executors over a `CleanupRig`.

The gateway, equivalence and continuity tests of the two merge flows all start the same way: a
signed approval row bound to the state digest of the rig's graph, then the real executor over the
rig's dependencies. Only the doubles are fakes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from authz._fakes import FakeAccessRoleStore

from graph_cleanup._fakes import (
    ABSORBED,
    OBL_ABSORBED,
    OBL_SURVIVOR,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    SURVIVOR,
)
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.executors import (
    TOOL_MERGE_CAPABILITIES,
    TOOL_MERGE_OBLIGATIONS,
    TOOL_MERGE_ROLES,
    execute_capability_merge,
    execute_obligation_merge,
    execute_role_merge,
)
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_obligation_merge_state,
    read_role_merge_state,
)
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.graph_cleanup.role_planner import plan_role_merge
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.graph_cleanup.models import (
        CapabilityMergePlan,
        ObligationMergePlan,
        RoleMergePlan,
    )

OWNER = ("owner", "https://issuer.example.com/")
OFFICER = ("officer", "https://issuer.example.com/")
CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)


def roles() -> FakeAccessRoleStore:
    """A role store where `OFFICER` holds `ComplianceOfficer`."""
    store = FakeAccessRoleStore(expected_owner=OWNER)
    store.bootstrap_first_owner(OWNER)
    store.grant(actor=OWNER, target=OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _row(
    tool: str, args: dict[str, object], display_summary: dict[str, object] | None = None
) -> PendingApprovalRow:
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=tool,
        normalized_args=args,
        actor_subject=OFFICER[0],
        actor_issuer=OFFICER[1],
        nonce=b"n",
        display_summary=display_summary or {},
        status="signed",
        outcome=None,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )


def capability_plan(rig: CleanupRig) -> CapabilityMergePlan:
    """The plan the preview computes from the rig's graph."""
    return plan_capability_merge(
        read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    )


def capability_row(rig: CleanupRig, *, acknowledged: bool = False) -> PendingApprovalRow:
    """A signed `merge-capabilities` approval bound to the rig's current state."""
    return _row(
        TOOL_MERGE_CAPABILITIES,
        {
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "acknowledge_governance_change": acknowledged,
            "state_digest": capability_plan(rig).state_digest,
        },
    )


def run_capability_merge(
    rig: CleanupRig,
    *,
    acknowledged: bool = False,
    row: PendingApprovalRow | None = None,
    dependencies: GraphCleanupDependencies | None = None,
) -> dict[str, object]:
    """Execute a signed capability merge (`row` when the test armed a read hook first)."""
    return execute_capability_merge(
        row or capability_row(rig, acknowledged=acknowledged),
        CONFIG,
        dependencies or rig.dependencies(roles()),
    )


def obligation_plan(rig: CleanupRig) -> ObligationMergePlan:
    """The plan the preview computes from the rig's graph."""
    return plan_obligation_merge(
        read_obligation_merge_state(rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)
    )


def obligation_row(rig: CleanupRig) -> PendingApprovalRow:
    """A signed `merge-obligations` approval bound to the rig's current state."""
    return _row(
        TOOL_MERGE_OBLIGATIONS,
        {
            "survivor_id": OBL_SURVIVOR,
            "absorbed_id": OBL_ABSORBED,
            "state_digest": obligation_plan(rig).state_digest,
        },
    )


def run_obligation_merge(
    rig: CleanupRig,
    *,
    row: PendingApprovalRow | None = None,
    dependencies: GraphCleanupDependencies | None = None,
) -> dict[str, object]:
    """Execute a signed obligation merge (`row` when the test armed a read hook first)."""
    return execute_obligation_merge(
        row or obligation_row(rig), CONFIG, dependencies or rig.dependencies(roles())
    )


def role_plan(rig: CleanupRig) -> RoleMergePlan:
    """The plan the preview computes from the rig's graph."""
    return plan_role_merge(
        read_role_merge_state(rig.read, survivor_id=ROLE_SURVIVOR, absorbed_id=ROLE_ABSORBED)
    )


def role_row(rig: CleanupRig, *, review_id: str | None = None) -> PendingApprovalRow:
    """A signed `merge-roles` approval bound to the rig's current state, with its preview.

    `review_id` is signed into the arguments the way the service does: only when set.
    """
    plan = role_plan(rig)
    return _row(
        TOOL_MERGE_ROLES,
        {
            "survivor_id": ROLE_SURVIVOR,
            "absorbed_id": ROLE_ABSORBED,
            "state_digest": plan.state_digest,
            **({"review_id": review_id} if review_id is not None else {}),
        },
        plan.preview.model_dump(mode="json"),
    )


def run_role_merge(
    rig: CleanupRig,
    *,
    row: PendingApprovalRow | None = None,
    dependencies: GraphCleanupDependencies | None = None,
) -> dict[str, object]:
    """Execute a signed role merge (`row` when the test armed a read hook first)."""
    return execute_role_merge(
        row or role_row(rig), CONFIG, dependencies or rig.dependencies(roles())
    )
