"""Executor for capability `unmerge`.

Audit-first and fail-closed like the merges: re-check the role, re-locate the merge in the
audit trail, re-read and compare the state digest, record `applied`, run the write, record `failed`
(same approval id) on a guard miss or a failed write.

The write is one gateway group (`CleanupRig`, the gateway over in-memory log and graph fakes);
the write-shape assertions name the group.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import ABSORBED, SURVIVOR, ScriptedUnmergeGraph
from graph_cleanup._merge_runs import CONFIG as _CONFIG
from graph_cleanup._merge_runs import OFFICER as _OFFICER
from graph_cleanup._merge_runs import OWNER as _OWNER
from graph_cleanup._unmerge_runs import (
    capability_unmerge_world,
    run_capability_unmerge,
    unmerge_row,
)
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.executors import (
    TOOL_UNMERGE,
    execute_unmerge,
    verify_unmerge_effect,
)
from ps_service.graph_gateway.models import DeleteEdge, UpsertEdge
from ps_service.logging import configure
from ps_service.passkey_signing.executors import resolve_approval_executor, resolve_effect_verifier

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.authz.store import AccessRoleStore
    from ps_service.passkey_signing.models import PendingApprovalRow

_NODE_READ = "RETURN c.id, c.name"
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
    capability_unmerge_world(rig)
    return rig


def _row(rig: CleanupRig, **args: object) -> PendingApprovalRow:
    return unmerge_row(rig, ABSORBED, **args)


def _deps(rig: CleanupRig, roles: AccessRoleStore | None = None) -> GraphCleanupDependencies:
    return rig.dependencies(roles if roles is not None else _roles())


def _survivor_gains_an_edge(rig: CleanupRig) -> None:
    rig.seed_endpoint("Obligation", "obl_new")
    rig.seed_edge(("REQUIRES", "Obligation", "obl_new", "Capability", SURVIVOR), legacy=False)


def _edge_keys(snapshot: object) -> set[tuple[str, str, str]]:
    edges = cast("dict[str, list[dict[str, str]]]", snapshot)["edges"]
    return {(e["rel_type"], e["source_id"], e["target_id"]) for e in edges}


def test_the_executor_and_verifier_are_registered_under_the_tool_name() -> None:
    assert TOOL_UNMERGE == "unmerge"
    assert resolve_approval_executor(TOOL_UNMERGE) is not None
    assert resolve_effect_verifier(TOOL_UNMERGE) is verify_unmerge_effect


def test_happy_path_audits_first_then_runs_the_write_once(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_unmerge(_row(rig), _CONFIG, _deps(rig))

    assert outcome == {
        "merged_id": ABSORBED,
        "survivor_id": SURVIVOR,
        "unmerged": True,
        "survivor_added_edges": [],
    }
    assert rig.order() == ["audit:applied", "log_append"]
    [group] = rig.hooked.groups
    restored = {p.identity for p in group.primitives if isinstance(p, UpsertEdge)}
    removed = {p.identity for p in group.primitives if isinstance(p, DeleteEdge)}
    assert {f"REQUIRES:obl_1->{ABSORBED}", f"REQUIRES:obl_2->{ABSORBED}"} <= restored
    assert f"REQUIRES:obl_1->{SURVIVOR}" in removed
    assert f"COVERS:pa_1->{SURVIVOR}" in removed


def test_the_audit_row_reverses_the_merge_and_lists_restored_and_survivor_added_edges(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    _survivor_gains_an_edge(rig)

    outcome = execute_unmerge(_row(rig), _CONFIG, _deps(rig))

    [recorded] = rig.audit.rows
    assert recorded.action == "capability.unmerge"
    assert recorded.resource_type == "capability"
    assert recorded.resource_id == ABSORBED
    assert recorded.outcome == "applied"
    assert (recorded.actor_subject, recorded.actor_issuer) == _OFFICER
    details = recorded.details
    assert details["survivor_id"] == SURVIVOR
    assert details["absorbed_id"] == ABSORBED
    assert details["approval_id"] == "approval-2"
    assert details["reverses_approval_id"] == "merge-approval-1"
    assert details["policy_case"] == 1
    assert ("MERGED_INTO", ABSORBED, SURVIVOR) in _edge_keys(details["before"])
    assert ("MERGED_INTO", ABSORBED, SURVIVOR) not in _edge_keys(details["after"])
    assert _edge_keys({"edges": details["restored_edges"]}) == {
        ("REQUIRES", "obl_1", ABSORBED),
        ("REQUIRES", "obl_2", ABSORBED),
        ("COVERS", "pa_1", ABSORBED),
        ("MITIGATED_BY", "rp_1", ABSORBED),
    }
    assert _edge_keys({"edges": details["survivor_added_edges"]}) == {
        ("REQUIRES", "obl_new", SURVIVOR)
    }
    assert [
        e["source_id"] for e in cast("list[dict[str, str]]", outcome["survivor_added_edges"])
    ] == ["obl_new"]


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
    assert failed.details["approval_id"] == "approval-2"
    assert failed.details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(outcome)
    assert "unmerged" not in outcome


def test_a_guard_miss_records_a_failed_row_and_reports_a_stale_graph(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    def _an_edge_disappears() -> None:
        del rig.graph.edges[
            next(k for k in rig.graph.edges if k[:3] == ("REQUIRES", "Obligation", "obl_1"))
        ]

    rig.read.flip_before(_NODE_READ, 2, _an_edge_disappears)

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
    _survivor_gains_an_edge(rig)

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "changed" in str(outcome["error"])


def test_an_approval_presented_for_a_different_merge_is_rejected(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_unmerge(_row(rig, merge_approval_id="some-other-merge"), _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "changed" in str(outcome["error"])


def test_an_approval_presented_for_a_different_id_is_rejected(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    row = replace(row, normalized_args={**row.normalized_args, "merged_id": "cap_elsewhere"})

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "error" in outcome


def test_a_conflict_that_arose_since_the_preview_is_rejected_with_its_explanation(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.graph.nodes[("Capability", SURVIVOR)]["status"] = "merged"
    rig.seed_capability("cap_winner", status="active")
    rig.seed_edge(("MERGED_INTO", "Capability", SURVIVOR, "Capability", "cap_winner"), legacy=False)

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "cap_winner" in str(outcome["error"])


def test_an_unreadable_audit_trail_is_a_generic_error_with_nothing_changed(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.audit.query_error = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert "10.1.2.3" not in str(outcome)
    assert "nothing was changed" in str(outcome["error"])


def test_an_unreachable_graph_is_a_generic_error_with_nothing_changed(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    unreachable = ScriptedUnmergeGraph(read_error=redis.exceptions.ConnectionError("host=10.1.2.3"))
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


@pytest.mark.parametrize(
    "bad_args",
    [{"merged_id": 1}, {"merge_approval_id": None}, {"state_digest": 3}, {"kind": "bogus"}],
)
def test_malformed_normalized_args_are_a_generic_error_with_no_work_done(
    cleanup_rig: CleanupRig, bad_args: dict[str, object]
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    row = replace(row, normalized_args={**row.normalized_args, **bad_args})

    outcome = execute_unmerge(row, _CONFIG, _deps(rig))

    assert "error" in outcome
    assert rig.logged_groups == []
    assert rig.audit.rows == []


def test_the_effect_verifier_needs_the_tombstone_to_be_gone(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    assert verify_unmerge_effect(row, rig.read) is False  # still a tombstone
    run_capability_unmerge(rig, row=row)
    assert verify_unmerge_effect(row, rig.read) is True  # active and no MERGED_INTO edge
