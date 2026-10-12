"""Executor for obligation `unmerge` (issue #190, slice 16 c; AC-BI-017/018/019/020/021/022).

Audit-first and fail-closed like the other executors: re-check the role, re-locate the merge in
the audit trail, re-read and compare the state digest, record `applied`, run the write, record
`failed` (same approval id) on a guard miss or a failed write.

Since #212 the write is one gateway group (`CleanupRig`, the gateway over in-memory log and graph
fakes); the assertions on outcomes, messages and audit rows are the ones from before that change,
only the write-shape ones (Cypher call recorded) now name the group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import OBL_ABSORBED, OBL_SURVIVOR, ScriptedObligationUnmergeGraph
from graph_cleanup._merge_runs import CONFIG as _CONFIG
from graph_cleanup._merge_runs import OFFICER as _OFFICER
from graph_cleanup._merge_runs import OWNER as _OWNER
from graph_cleanup._unmerge_runs import (
    obligation_unmerge_world,
    run_obligation_unmerge,
    unmerge_row,
)
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.executors import execute_unmerge, verify_unmerge_effect
from ps_service.graph_gateway.models import UpsertEdge, UpsertNode
from ps_service.logging import configure

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.authz.store import AccessRoleStore
    from ps_service.passkey_signing.models import PendingApprovalRow

_NODE_READ = "RETURN o.id, o.text"
"""The first run of this read is the replan, the second the write step's guard read."""


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _roles(*, officer: bool = True) -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    if officer:
        store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _world(rig: CleanupRig) -> CleanupRig:
    obligation_unmerge_world(rig)
    return rig


def _row(rig: CleanupRig, **args: object) -> PendingApprovalRow:
    return unmerge_row(rig, OBL_ABSORBED, **args)


def _deps(rig: CleanupRig, roles: AccessRoleStore | None = None) -> GraphCleanupDependencies:
    return rig.dependencies(roles if roles is not None else _roles())


def _survivor_gains_a_capability(rig: CleanupRig) -> None:
    rig.seed_endpoint("Capability", "cap_new", name="cap_new", status="active")
    rig.seed_edge(("REQUIRES", "Obligation", OBL_SURVIVOR, "Capability", "cap_new"), legacy=False)


def _edge_keys(edges: object) -> set[tuple[str, str, str]]:
    return {
        (e["rel_type"], e["source_id"], e["target_id"]) for e in cast("list[dict[str, str]]", edges)
    }


def test_happy_path_audits_first_then_runs_the_write_once(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_unmerge(_row(rig), _CONFIG, _deps(rig))

    assert outcome["merged_id"] == OBL_ABSORBED
    assert outcome["survivor_id"] == OBL_SURVIVOR
    assert outcome["unmerged"] is True
    assert rig.order() == ["audit:applied", "log_append"]
    [group] = rig.hooked.groups
    [node] = [p for p in group.primitives if isinstance(p, UpsertNode)]
    assert node.id == OBL_ABSORBED
    assert node.properties == {"confidence": 0.8, "text": "Report  incidents."}
    identities = {p.identity for p in group.primitives if isinstance(p, UpsertEdge)}
    assert identities == {
        f"HAS:role_1->{OBL_ABSORBED}",
        f"SATISFIED_BY:req_1->{OBL_ABSORBED}",
        f"SATISFIED_BY:req_2->{OBL_ABSORBED}",
        f"REQUIRES:{OBL_ABSORBED}->cap_1",
    }


def test_the_audit_row_reverses_the_merge_and_lists_the_survivors_edges(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    _survivor_gains_a_capability(rig)

    outcome = execute_unmerge(_row(rig), _CONFIG, _deps(rig))

    [recorded] = rig.audit.rows
    assert recorded.action == "obligation.unmerge"
    assert recorded.resource_type == "obligation"
    assert recorded.resource_id == OBL_ABSORBED
    assert recorded.outcome == "applied"
    assert (recorded.actor_subject, recorded.actor_issuer) == _OFFICER
    details = recorded.details
    assert details["survivor_id"] == OBL_SURVIVOR
    assert details["absorbed_id"] == OBL_ABSORBED
    assert details["role_id"] == "role_1"
    assert details["approval_id"] == "approval-2"
    assert details["reverses_approval_id"] == "merge-approval-1"
    after_edges = cast("dict[str, list[dict[str, str]]]", details["after"])["edges"]
    assert ("HAS", "role_1", OBL_ABSORBED) in _edge_keys(after_edges)
    assert _edge_keys(details["restored_edges"]) >= {
        ("SATISFIED_BY", "req_1", OBL_ABSORBED),
        ("REQUIRES", OBL_ABSORBED, "cap_1"),
    }
    assert _edge_keys(details["survivor_added_edges"]) == {("REQUIRES", OBL_SURVIVOR, "cap_new")}
    assert _edge_keys(details["survivor_edges_possibly_from_merge"]) == {
        ("SATISFIED_BY", "req_1", OBL_SURVIVOR)
    }
    assert _edge_keys(outcome["survivor_added_edges"]) == {("REQUIRES", OBL_SURVIVOR, "cap_new")}


def test_audit_failure_means_no_graph_write_at_all(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "10.1.2.3" not in str(outcome)
    assert outcome["error"]


def test_audit_persistence_failure_also_blocks_the_write(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.audit.raise_on_outcome["applied"] = AuditPersistenceError("insert failed: relation x")

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert "relation" not in str(outcome)


def test_a_graph_failure_records_a_failed_row_with_the_same_approval_id_and_no_detail(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.store.fail_appends()

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert [r.outcome for r in rig.audit.rows] == ["applied", "failed"]
    failed = rig.audit.rows[1]
    assert failed.action == "obligation.unmerge"
    assert failed.details["approval_id"] == "approval-2"
    assert failed.details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(outcome)
    assert "unmerged" not in outcome


def test_a_guard_miss_records_a_failed_row_and_reports_a_stale_graph(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    def _a_requirement_disappears() -> None:
        del rig.graph.nodes[("Requirement", "req_1")]

    rig.read.flip_before(_NODE_READ, 2, _a_requirement_disappears)

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

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

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert "error" in outcome
    assert [r.outcome for r in rig.audit.rows] == ["applied"]


def test_state_drift_since_the_preview_is_stale_with_no_audit_row_and_no_write(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    _survivor_gains_a_capability(rig)

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "changed" in str(outcome["error"])


def test_an_approval_presented_for_a_different_merge_or_kind_is_rejected(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    for override in ({"merge_approval_id": "some-other-merge"}, {"kind": "capability"}):
        outcome = execute_unmerge(_row(rig, **override), _CONFIG, _deps(rig))

        assert rig.logged_groups == []
        assert rig.audit.rows == []
        assert "changed" in str(outcome["error"])


def test_a_conflict_that_arose_since_the_preview_is_rejected_with_its_explanation(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    del rig.graph.nodes[("Obligation", OBL_SURVIVOR)]
    rig.seed_endpoint("MergedObligation", OBL_SURVIVOR, merged_into="obl_winner")

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "obl_winner" in str(outcome["error"])


def test_an_obligation_that_exists_again_is_rejected_before_any_audit_or_write(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.seed_endpoint("Obligation", OBL_ABSORBED, text="regenerated", confidence=0.1)

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "already exists" in str(outcome["error"])


def test_a_revoked_compliance_officer_cannot_execute(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    outcome = execute_unmerge(row, _CONFIG, _deps(rig, _roles(officer=False)))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "error" in outcome


def test_an_authorization_store_outage_fails_closed(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    outcome = execute_unmerge(row, _CONFIG, _deps(rig, RaisingAccessRoleStore()))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "error" in outcome


def test_an_unreachable_graph_is_a_generic_error_with_nothing_changed(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = replace(_row(rig))
    unreachable = ScriptedObligationUnmergeGraph(
        read_error=redis.exceptions.ConnectionError("host=10.1.2.3")
    )
    dependencies = GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: unreachable,
        audit_store=lambda _config: rig.audit,
        access_role_store=lambda _config: _roles(),
        graph_write_gateway=_deps(rig).graph_write_gateway,
    )

    outcome = execute_unmerge(row, _CONFIG, dependencies)

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "10.1.2.3" not in str(outcome)


def test_the_effect_verifier_needs_the_obligation_back_and_the_marker_gone(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    assert verify_unmerge_effect(row, rig.read) is False  # marker, node absent
    rig.seed_endpoint("Obligation", OBL_ABSORBED, text="back")
    assert verify_unmerge_effect(row, rig.read) is False  # marker, node present
    del rig.graph.nodes[("MergedObligation", OBL_ABSORBED)]
    assert verify_unmerge_effect(row, rig.read) is True  # node present, marker gone
    del rig.graph.nodes[("Obligation", OBL_ABSORBED)]
    assert verify_unmerge_effect(row, rig.read) is False  # neither


def test_running_the_executor_leaves_the_effect_the_verifier_looks_for(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    run_obligation_unmerge(rig, row=row)

    assert verify_unmerge_effect(row, rig.read) is True
