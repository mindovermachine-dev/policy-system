"""Executor for merge case 2 (issue #190, slice 12 c; AC-BI-010, AC-BI-021, M3).

Defence in depth: a case-2 approval whose signed arguments lack the acknowledgment is refused
at execution even if someone crafted the row. The audit row records policy case 2,
acknowledgment, the policy id/status and the before/after governed set, and its snapshots
carry the moved `GOVERNED_BY` edge. Since #212 the merge is one gateway group (`CleanupRig`).
"""

from __future__ import annotations

from dataclasses import replace
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


def _world(rig: CleanupRig, *, governed: str = ABSORBED, status: str = "approved") -> CleanupRig:
    rig.seed_capability_merge_world(governed=((governed, POLICY, status),))
    return rig


def _row(rig: CleanupRig, *, acknowledged: bool = True) -> PendingApprovalRow:
    state = read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    digest = plan_capability_merge(state).state_digest
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-2",
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


def test_an_acknowledged_case_two_merge_audits_first_then_writes_once(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_capability_merge(_row(rig), _CONFIG, _deps(rig))

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    assert rig.order() == ["audit:applied", "log_append"]
    assert len(rig.hooked.groups) == 1


def test_the_plan_the_write_is_guarded_by_pins_the_governing_policy_and_its_status(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig, status="approved")

    expected = plan_capability_merge(
        read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    ).expected

    assert expected.absorbed_governed == 1
    assert expected.survivor_governed == 0
    assert expected.absorbed_policy_id == POLICY
    assert expected.absorbed_policy_status == "approved"


def test_the_policy_edge_moves_to_the_survivor_in_the_graph(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    execute_capability_merge(_row(rig), _CONFIG, _deps(rig))

    governed = {(k[2], k[4]) for k in rig.graph.edges if k[0] == "GOVERNED_BY"}
    assert governed == {(SURVIVOR, POLICY), ("cap_other", POLICY)}


def test_the_audit_row_records_case_two_acknowledgment_policy_and_governed_sets(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    execute_capability_merge(_row(rig), _CONFIG, _deps(rig))

    details = rig.audit.rows[0].details
    assert details["policy_case"] == 2
    assert details["acknowledged"] is True
    assert details["policy_id"] == "pol_1"
    assert details["policy_status"] == "approved"
    assert details["governed_set_before"] == [ABSORBED, "cap_other"]
    assert details["governed_set_after"] == ["cap_other", SURVIVOR]


def test_the_snapshots_carry_the_moved_governed_by_edge(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    execute_capability_merge(_row(rig), _CONFIG, _deps(rig))

    details = rig.audit.rows[0].details
    assert ("GOVERNED_BY", ABSORBED, "pol_1") in _edge_keys(details["before"])
    assert ("GOVERNED_BY", SURVIVOR, "pol_1") in _edge_keys(details["after"])
    assert ("GOVERNED_BY", ABSORBED, "pol_1") not in _edge_keys(details["after"])


def test_a_governed_survivor_keeps_its_edge_and_is_still_case_two(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig, governed=SURVIVOR, status="draft")

    execute_capability_merge(_row(rig), _CONFIG, _deps(rig))

    details = rig.audit.rows[0].details
    assert details["policy_case"] == 2
    assert details["policy_status"] == "draft"
    assert ("GOVERNED_BY", SURVIVOR, "pol_1") in _edge_keys(details["before"])
    assert ("GOVERNED_BY", SURVIVOR, "pol_1") in _edge_keys(details["after"])


def test_a_crafted_case_two_row_without_the_acknowledgment_is_refused_with_no_audit_or_write(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_capability_merge(_row(rig, acknowledged=False), _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "acknowledg" in str(outcome["error"])


@pytest.mark.parametrize("flag", [None, "true", 1, 0])
def test_only_a_literal_true_acknowledgment_counts(cleanup_rig: CleanupRig, flag: object) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    row = replace(
        row, normalized_args={**row.normalized_args, "acknowledge_governance_change": flag}
    )

    outcome = execute_capability_merge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert "error" in outcome


def test_a_policy_status_change_since_the_preview_is_rejected_before_any_audit_or_write(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig, status="approved")
    row = _row(rig)
    rig.graph.nodes[("Policy", POLICY)]["status"] = "deprecated"

    outcome = execute_capability_merge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "changed" in str(outcome["error"])


def test_a_failed_case_two_row_keeps_the_case_and_the_acknowledgment(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.read.flip_before(
        _NODE_READ, 2, lambda: rig.graph.nodes[("Policy", POLICY)].update(status="deprecated")
    )

    execute_capability_merge(row, _CONFIG, _deps(rig))

    failed = rig.audit.rows[1]
    assert failed.outcome == "failed"
    assert failed.details["policy_case"] == 2
    assert failed.details["acknowledged"] is True
    assert failed.details["reason_code"] == "graph_guard_missed"
