"""Executor for merge case 3, same policy.

No acknowledgment is needed; the audit row records policy case 3 with `acknowledged` false,
the policy id/status and the before/after governed set; the snapshots show the absorbed
`GOVERNED_BY` edge deleted and the survivor's kept; the guard pins both sides to the policy.
The merge is one gateway group (`CleanupRig`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore

from graph_cleanup._fakes import ABSORBED, SURVIVOR
from graph_cleanup._gateway_fakes import POLICY, CleanupRig
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.executors import TOOL_MERGE_CAPABILITIES, execute_capability_merge
from ps_service.graph_cleanup.graph_reader import read_capability_merge_state
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.logging import configure
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies

_OWNER = ("owner", "https://issuer.example.com/")
_OFFICER = ("officer", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _roles() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


_NODE_READ = "RETURN c.id, c.name"


def _world(rig: CleanupRig, status: str = "approved") -> CleanupRig:
    rig.seed_capability_merge_world(
        governed=((SURVIVOR, POLICY, status), (ABSORBED, POLICY, status))
    )
    return rig


def _row(rig: CleanupRig, *, acknowledged: bool = False) -> PendingApprovalRow:
    state = read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    digest = plan_capability_merge(state).state_digest
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-3",
        code_hash=b"h",
        tool_name=TOOL_MERGE_CAPABILITIES,
        normalized_args={
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "acknowledge_governance_change": acknowledged,
            "state_digest": digest,
        },
        actor_subject=_OFFICER[0],
        actor_issuer=_OFFICER[1],
        nonce=b"n",
        display_summary={},
        status="signed",
        outcome=None,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )


def _deps(rig: CleanupRig) -> GraphCleanupDependencies:
    return rig.dependencies(_roles())


def _edge_keys(snapshot: object) -> set[tuple[str, str, str]]:
    edges = cast("dict[str, list[dict[str, str]]]", snapshot)["edges"]
    return {(e["rel_type"], e["source_id"], e["target_id"]) for e in edges}


def test_a_same_policy_merge_runs_without_an_acknowledgment_audit_first(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_capability_merge(_row(rig, acknowledged=False), _CONFIG, _deps(rig))

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    assert rig.order() == ["audit:applied", "log_append"]
    assert len(rig.hooked.groups) == 1


def test_the_audit_row_records_case_three_not_acknowledged_policy_and_governed_sets(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    execute_capability_merge(_row(rig), _CONFIG, _deps(rig))

    details = rig.audit.rows[0].details
    assert details["policy_case"] == 3
    assert details["acknowledged"] is False
    assert details["policy_id"] == "pol_1"
    assert details["policy_status"] == "approved"
    assert details["governed_set_before"] == [ABSORBED, "cap_other", SURVIVOR]
    assert details["governed_set_after"] == ["cap_other", SURVIVOR]


def test_the_snapshots_show_the_absorbed_edge_deleted_and_the_survivors_kept(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    execute_capability_merge(_row(rig), _CONFIG, _deps(rig))

    details = rig.audit.rows[0].details
    assert ("GOVERNED_BY", ABSORBED, "pol_1") in _edge_keys(details["before"])
    assert ("GOVERNED_BY", SURVIVOR, "pol_1") in _edge_keys(details["before"])
    assert ("GOVERNED_BY", ABSORBED, "pol_1") not in _edge_keys(details["after"])
    assert ("GOVERNED_BY", SURVIVOR, "pol_1") in _edge_keys(details["after"])


def test_the_plan_the_write_is_guarded_by_pins_both_sides_to_the_one_policy_and_its_status(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig, status="draft")

    expected = plan_capability_merge(
        read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    ).expected

    assert expected.absorbed_governed == 1
    assert expected.survivor_governed == 1
    assert expected.absorbed_policy_id == expected.survivor_policy_id == "pol_1"
    assert expected.absorbed_policy_status == "draft"
    assert expected.survivor_policy_status == "draft"


def test_the_absorbed_edge_is_deleted_and_the_survivors_is_kept_in_the_graph(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    execute_capability_merge(_row(rig), _CONFIG, _deps(rig))

    governed = {(k[2], k[4]) for k in rig.graph.edges if k[0] == "GOVERNED_BY"}
    assert governed == {(SURVIVOR, POLICY), ("cap_other", POLICY)}


def test_a_policy_swapped_for_a_different_one_since_the_preview_is_rejected_unaudited(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    for key in [k for k in rig.graph.edges if k[:3] == ("GOVERNED_BY", "Capability", ABSORBED)]:
        del rig.graph.edges[key]
    rig.seed_policy("pol_2", title="Other Policy", status="approved")
    rig.seed_governs(ABSORBED, "pol_2")

    outcome = execute_capability_merge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    [recorded] = rig.audit.rows
    assert recorded.outcome == "failed"
    assert recorded.details["reason_code"] == "revalidation_rejected"
    assert "release-capability-governance" in str(outcome["error"])


def test_a_guard_miss_records_a_failed_row_that_keeps_case_three(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.read.flip_before(
        _NODE_READ, 2, lambda: rig.graph.nodes[("Policy", POLICY)].update(status="deprecated")
    )

    execute_capability_merge(row, _CONFIG, _deps(rig))

    failed = rig.audit.rows[1]
    assert failed.outcome == "failed"
    assert failed.details["policy_case"] == 3
    assert failed.details["acknowledged"] is False
    assert failed.details["reason_code"] == "graph_guard_missed"
