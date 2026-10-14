"""Every Graph Cleanup group requests a digest checkpoint.

Each of the five cleanup operations is one group on `policy_system`, so each is its own terminal
group. The real executors run over the `CleanupRig`; a checkpoint that cannot be recorded never
fails the operation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from graph_gateway._fakes import Fault
from graph_gateway.checkpoint_checks import assert_head_checkpoint, assert_no_checkpoint

from graph_cleanup._fakes import SURVIVOR
from graph_cleanup._gateway_fakes import POLICY, POLICY_SYSTEM, CleanupRig
from graph_cleanup._merge_runs import (
    CONFIG,
    OFFICER,
    roles,
    run_capability_merge,
    run_obligation_merge,
)
from graph_cleanup._unmerge_runs import (
    capability_unmerge_world,
    obligation_unmerge_world,
    run_capability_unmerge,
    run_obligation_unmerge,
)
from ps_service.graph_cleanup.executors import TOOL_RELEASE_GOVERNANCE, execute_release_governance
from ps_service.graph_cleanup.graph_reader import read_release_state
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Callable


def _release(rig: CleanupRig) -> None:
    rig.seed_release_world()
    state = read_release_state(rig.read, capability_id=SURVIVOR)
    now = datetime.now(UTC)
    row = PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=TOOL_RELEASE_GOVERNANCE,
        normalized_args={
            "capability_id": SURVIVOR,
            "policy_id": POLICY,
            "state_digest": plan_release_governance(state).state_digest,
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
    execute_release_governance(row, CONFIG, rig.dependencies(roles()))


def _capability_merge(rig: CleanupRig) -> None:
    rig.seed_capability_merge_world()
    run_capability_merge(rig)


def _obligation_merge(rig: CleanupRig) -> None:
    rig.seed_obligation_merge_world()
    run_obligation_merge(rig)


def _capability_unmerge(rig: CleanupRig) -> None:
    capability_unmerge_world(rig)
    run_capability_unmerge(rig)


def _obligation_unmerge(rig: CleanupRig) -> None:
    obligation_unmerge_world(rig)
    run_obligation_unmerge(rig)


_OPERATIONS: dict[str, Callable[[CleanupRig], None]] = {
    "release": _release,
    "merge-capabilities": _capability_merge,
    "merge-obligations": _obligation_merge,
    "unmerge-capability": _capability_unmerge,
    "unmerge-obligation": _obligation_unmerge,
}


@pytest.mark.parametrize("operation", list(_OPERATIONS))
def test_a_cleanup_leaves_a_head_checkpoint_on_policy_system(
    operation: str, cleanup_rig: CleanupRig
) -> None:
    _OPERATIONS[operation](cleanup_rig)

    assert cleanup_rig.cleanup_groups(), "the operation logged no group"
    assert_head_checkpoint(cleanup_rig, POLICY_SYSTEM)


@pytest.mark.parametrize("operation", list(_OPERATIONS))
def test_a_checkpoint_that_cannot_be_recorded_does_not_fail_the_cleanup(
    operation: str, cleanup_rig: CleanupRig
) -> None:
    cleanup_rig.store.checkpoint_fault = Fault(redis.exceptions.ConnectionError("down"))

    _OPERATIONS[operation](cleanup_rig)

    assert cleanup_rig.cleanup_groups(), "the operation logged no group"
    assert_no_checkpoint(cleanup_rig)
