"""A signed `merge-roles` approval whose outcome was never recorded is settled lazily.

`check-cleanup-approval` reconciles it past the grace window: effect present (the absorbed Role
gone and its `MergedRole` marker naming the survivor) -> outcome reconciled as applied; effect
absent -> a `failed` `role.merge` row (`interrupted_no_effect`, same approval id) and a failed
outcome.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from graph_cleanup._fakes import (
    INSTRUMENT_ID,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    FakeApprovalStore,
    RecordingAuditStore,
    expire_approval,
)
from graph_cleanup._merge_runs import run_role_merge
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.audit_actions import RoleMergeDetails
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.service import (
    RECONCILE_GRACE,
    check_cleanup_approval,
    create_role_merge_approval,
)

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig

_ACTOR = ("officer", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)


def _orphan(rig: CleanupRig) -> tuple[FakeApprovalStore, str]:
    """A signed, expired `merge-roles` approval created from the rig's current graph."""
    store = FakeApprovalStore()
    approval = create_role_merge_approval(
        rig.read,
        survivor_id=ROLE_SURVIVOR,
        absorbed_id=ROLE_ABSORBED,
        actor=_ACTOR,
        base_url="https://ps.example.com",
        store=store,
    )
    assert store.mark_signed(approval.pending_approval_id)
    expire_approval(
        store, approval.pending_approval_id, expired_for=RECONCILE_GRACE + timedelta(seconds=5)
    )
    return store, approval.pending_approval_id


def _check(rig: CleanupRig, store: FakeApprovalStore, approval_id: str, audit: RecordingAuditStore):
    return check_cleanup_approval(
        pending_approval_id=approval_id,
        actor=_ACTOR,
        store=store,
        config=_CONFIG,
        dependencies=GraphCleanupDependencies(
            open_single_tenant_graph=lambda _c: rig.read,
            audit_store=lambda _c: audit,
            graph_write_gateway=lambda _c: rig.gateway,
        ),
    )


def test_an_orphan_whose_merge_never_ran_gets_a_failed_role_merge_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    store, approval_id = _orphan(rig)
    audit = RecordingAuditStore()

    status = _check(rig, store, approval_id, audit)

    assert status is not None
    assert status.outcome is not None
    assert "error" in status.outcome
    [failed] = audit.rows
    assert (failed.action, failed.resource_type, failed.resource_id) == (
        "role.merge",
        "role",
        ROLE_ABSORBED,
    )
    assert failed.outcome == "failed"
    details = RoleMergeDetails.model_validate(failed.details)
    assert details.reason_code == "interrupted_no_effect"
    assert details.approval_id == approval_id
    assert (details.survivor_id, details.absorbed_id) == (ROLE_SURVIVOR, ROLE_ABSORBED)
    assert details.instrument_id == INSTRUMENT_ID


def test_an_orphan_whose_merge_ran_is_reconciled_as_applied_with_no_extra_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    store, approval_id = _orphan(rig)
    run_role_merge(rig)
    audit = RecordingAuditStore()

    status = _check(rig, store, approval_id, audit)

    assert status is not None
    assert status.outcome == {"reconciled": "applied"}
    assert audit.rows == []
