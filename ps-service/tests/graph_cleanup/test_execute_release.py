"""Executor for `release-capability-governance` (issue #190, slice 14 c; AC-BI-013/021/022).

Audit-first and fail-closed like the merges: re-check the role, re-read and compare the state
digest, record `applied`, delete the edge, record `failed` (same approval id) on a guard miss
or write failure. The audit row carries the before/after snapshot and the governed sets.

Since #212 the edge is deleted by one gateway group (`CleanupRig`, the gateway over in-memory
log and graph fakes); the assertions on outcomes, messages and audit rows are the ones from before
that change, only the write-shape ones (Cypher call recorded) now name the group.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import SURVIVOR
from graph_cleanup._gateway_fakes import POLICY, CleanupRig
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.executors import (
    TOOL_RELEASE_GOVERNANCE,
    execute_release_governance,
    verify_release_governance_effect,
)
from ps_service.graph_cleanup.graph_reader import read_release_state
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.graph_gateway.errors import GraphWriteRejectedError
from ps_service.graph_gateway.models import DeleteEdge
from ps_service.logging import configure
from ps_service.passkey_signing.executors import resolve_approval_executor, resolve_effect_verifier
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from ps_service.authz.store import AccessRoleStore
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


def _roles(*, officer: bool = True) -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    if officer:
        store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _row(rig: CleanupRig, **args: object) -> PendingApprovalRow:
    state = read_release_state(rig.read, capability_id=SURVIVOR)
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=TOOL_RELEASE_GOVERNANCE,
        normalized_args={
            "capability_id": SURVIVOR,
            "policy_id": "pol_1",
            "state_digest": plan_release_governance(state).state_digest,
            **args,
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


def _deps(rig: CleanupRig, roles: AccessRoleStore | None = None) -> GraphCleanupDependencies:
    return rig.dependencies(roles if roles is not None else _roles())


def _world(rig: CleanupRig) -> CleanupRig:
    rig.seed_release_world()
    return rig


def _release_edges(rig: CleanupRig) -> set[tuple[str, str]]:
    return {(k[2], k[4]) for k in rig.graph.edges if k[0] == "GOVERNED_BY"}


def _forget_the_governing_edge(rig: CleanupRig) -> None:
    for key in [
        k
        for k in rig.graph.edges
        if k[:5] == ("GOVERNED_BY", "Capability", SURVIVOR, "Policy", POLICY)
    ]:
        del rig.graph.edges[key]


def _edge_keys(snapshot: object) -> set[tuple[str, str, str]]:
    edges = cast("dict[str, list[dict[str, str]]]", snapshot)["edges"]
    return {(e["rel_type"], e["source_id"], e["target_id"]) for e in edges}


def test_the_executor_and_verifier_are_registered_under_the_tool_name() -> None:
    assert TOOL_RELEASE_GOVERNANCE == "release-capability-governance"
    assert resolve_approval_executor(TOOL_RELEASE_GOVERNANCE) is not None
    assert resolve_effect_verifier(TOOL_RELEASE_GOVERNANCE) is verify_release_governance_effect


def test_happy_path_audits_first_then_deletes_the_edge_once(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_release_governance(_row(rig), _CONFIG, _deps(rig))

    assert outcome == {"capability_id": SURVIVOR, "policy_id": "pol_1", "released": True}
    assert rig.order() == ["audit:applied", "log_append"]
    assert len(rig.hooked.groups) == 1
    [edge_delete] = rig.hooked.groups[0].primitives
    assert isinstance(edge_delete, DeleteEdge)
    assert (edge_delete.op, edge_delete.source.id, edge_delete.target.id) == (
        "delete_edge",
        SURVIVOR,
        "pol_1",
    )
    assert _release_edges(rig) == {("cap_other", "pol_1")}


def test_the_audit_row_has_the_snapshot_the_governed_sets_and_the_approval_id(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    execute_release_governance(_row(rig), _CONFIG, _deps(rig))

    [recorded] = rig.audit.rows
    assert recorded.action == "capability.release_governance"
    assert recorded.resource_type == "capability"
    assert recorded.resource_id == SURVIVOR
    assert recorded.outcome == "applied"
    assert (recorded.actor_subject, recorded.actor_issuer) == _OFFICER
    details = recorded.details
    assert details["capability_id"] == SURVIVOR
    assert details["policy_id"] == "pol_1"
    assert details["policy_status"] == "draft"
    assert details["approval_id"] == "approval-1"
    assert ("GOVERNED_BY", SURVIVOR, "pol_1") in _edge_keys(details["before"])
    assert ("GOVERNED_BY", SURVIVOR, "pol_1") not in _edge_keys(details["after"])
    assert details["governed_set_before"] == ["cap_other", SURVIVOR]
    assert details["governed_set_after"] == ["cap_other"]


def test_audit_failure_means_no_graph_write_at_all(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    rig.audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_release_governance(_row(rig), _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "10.1.2.3" not in str(outcome)
    assert outcome["error"]


def test_audit_persistence_failure_also_blocks_the_write(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    rig.audit.raise_on_outcome["applied"] = AuditPersistenceError("insert failed: relation x")

    outcome = execute_release_governance(_row(rig), _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert "relation" not in str(outcome)


def test_a_write_failure_records_a_failed_row_with_the_same_approval_id(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.store.fail_appends(error=GraphWriteRejectedError("host=10.1.2.3 port=6379 refused"))

    outcome = execute_release_governance(row, _CONFIG, _deps(rig))

    assert [r.outcome for r in rig.audit.rows] == ["applied", "failed"]
    failed = rig.audit.rows[1]
    assert failed.details["approval_id"] == "approval-1"
    assert failed.details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(outcome)
    assert "released" not in outcome
    assert _release_edges(rig) == {(SURVIVOR, "pol_1"), ("cap_other", "pol_1")}


def test_a_guard_miss_records_a_failed_row_and_reports_a_stale_graph(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.read.flip_before("p.title", 2, lambda: _forget_the_governing_edge(rig))

    outcome = execute_release_governance(row, _CONFIG, _deps(rig))

    assert [r.outcome for r in rig.audit.rows] == ["applied", "failed"]
    assert rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert "changed" in str(outcome["error"])
    assert rig.logged_groups == []


def test_a_failed_row_that_cannot_be_recorded_does_not_mask_the_error(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.store.fail_appends()
    rig.audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("down")

    outcome = execute_release_governance(row, _CONFIG, _deps(rig))

    assert "error" in outcome
    assert [r.outcome for r in rig.audit.rows] == ["applied"]


def test_a_policy_that_left_draft_since_the_preview_is_rejected_before_any_audit_or_write(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.graph.nodes[("Policy", "pol_1")]["status"] = "approved"

    outcome = execute_release_governance(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "policy lifecycle" in str(outcome["error"])


def test_a_released_or_regoverned_capability_since_the_preview_is_stale(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    _forget_the_governing_edge(rig)
    rig.seed_policy("pol_2", title="Other Policy", status="draft")
    rig.seed_governs(SURVIVOR, "pol_2")

    outcome = execute_release_governance(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "changed" in str(outcome["error"])


def test_a_policy_id_that_differs_from_the_governing_policy_is_refused(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_release_governance(_row(rig, policy_id="pol_other"), _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "changed" in str(outcome["error"])


def test_a_revoked_compliance_officer_cannot_execute(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_release_governance(_row(rig), _CONFIG, _deps(rig, _roles(officer=False)))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "error" in outcome


def test_an_authorization_store_outage_fails_closed(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_release_governance(_row(rig), _CONFIG, _deps(rig, RaisingAccessRoleStore()))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "error" in outcome


@pytest.mark.parametrize(
    "bad_args",
    [{"capability_id": 1}, {"policy_id": None}, {"state_digest": 3}],
)
def test_malformed_normalized_args_are_a_generic_error_with_no_work_done(
    cleanup_rig: CleanupRig, bad_args: dict[str, object]
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    row = replace(row, normalized_args={**row.normalized_args, **bad_args})

    outcome = execute_release_governance(row, _CONFIG, _deps(rig))

    assert "error" in outcome
    assert rig.logged_groups == []
    assert rig.audit.rows == []


def test_the_effect_verifier_needs_the_edge_to_be_gone(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    assert verify_release_governance_effect(row, rig.read) is False
    _forget_the_governing_edge(rig)
    assert verify_release_governance_effect(row, rig.read) is True
