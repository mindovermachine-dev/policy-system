"""Executor for `merge-obligations`.

Audit-first ordering, fail-closed: re-check the actor's role; re-read and re-validate against
the previewed state digest; record the `applied` audit row; only then write; a guard miss or
graph failure records a `failed` row with the same `approval_id` and returns a generic error.
The merge is one gateway group (`CleanupRig`, the gateway over in-memory log and graph fakes); the
write-shape assertions name the group.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import OBL_ABSORBED, OBL_SURVIVOR, ROLE_ID
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.executors import (
    TOOL_MERGE_OBLIGATIONS,
    execute_obligation_merge,
    verify_obligation_merge_effect,
)
from ps_service.graph_cleanup.graph_reader import read_obligation_merge_state
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
from ps_service.graph_gateway.errors import GraphWriteRejectedError
from ps_service.logging import configure
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.authz.store import AccessRoleStore
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies

_NODE_READ = "RETURN o.id, o.text"
_NEW_REQUIRES = ("REQUIRES", "Obligation", OBL_ABSORBED, "Capability", "cap_new")

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


def _digest(rig: CleanupRig) -> str:
    state = read_obligation_merge_state(
        rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED
    )
    return plan_obligation_merge(state).state_digest


def _row(rig: CleanupRig, **args: object) -> PendingApprovalRow:
    now = datetime.now(UTC)
    normalized: dict[str, object] = {
        "survivor_id": OBL_SURVIVOR,
        "absorbed_id": OBL_ABSORBED,
        "state_digest": _digest(rig),
        **args,
    }
    return PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=TOOL_MERGE_OBLIGATIONS,
        normalized_args=normalized,
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
    rig.seed_obligation_merge_world()
    rig.seed_endpoint("Capability", "cap_new", name="cap_new", status="active")
    return rig


def _edge_keys(snapshot: object) -> set[tuple[str, str, str]]:
    edges = cast("dict[str, list[dict[str, str]]]", snapshot)["edges"]
    return {(e["rel_type"], e["source_id"], e["target_id"]) for e in edges}


def test_happy_path_audits_first_then_writes_once_and_reports_the_merge(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))

    assert outcome == {"survivor_id": OBL_SURVIVOR, "absorbed_id": OBL_ABSORBED, "merged": True}
    assert rig.order() == ["audit:applied", "log_append"]
    assert len(rig.hooked.groups) == 1
    [recorded] = rig.audit.rows
    assert recorded.action == "obligation.merge"
    assert recorded.resource_type == "obligation"
    assert recorded.resource_id == OBL_ABSORBED
    assert recorded.outcome == "applied"
    assert (recorded.actor_subject, recorded.actor_issuer) == _OFFICER
    details = recorded.details
    assert details["survivor_id"] == OBL_SURVIVOR
    assert details["absorbed_id"] == OBL_ABSORBED
    assert details["role_id"] == "role_1"
    assert details["approval_id"] == "approval-1"
    assert details["before"] and details["after"]


def test_the_audit_row_holds_the_absorbed_node_in_full_and_every_edge(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)

    execute_obligation_merge(_row(rig), _CONFIG, _deps(rig))

    details = rig.audit.rows[0].details
    before = cast("dict[str, list[dict[str, object]]]", details["before"])
    absorbed = next(n for n in before["nodes"] if n["id"] == OBL_ABSORBED)
    assert absorbed["properties"] == {"text": "Report  incidents.", "confidence": 0.8}
    before_edges = _edge_keys(details["before"])
    assert {
        ("HAS", "role_1", OBL_ABSORBED),
        ("SATISFIED_BY", "req_1", OBL_ABSORBED),
        ("SATISFIED_BY", "req_2", OBL_ABSORBED),
        ("REQUIRES", OBL_ABSORBED, "cap_1"),
    } <= before_edges
    after = cast("dict[str, list[dict[str, object]]]", details["after"])
    assert any(n["label"] == "MergedObligation" and n["id"] == OBL_ABSORBED for n in after["nodes"])
    assert ("SATISFIED_BY", "req_1", OBL_SURVIVOR) in _edge_keys(details["after"])


def test_audit_failure_means_no_graph_write_at_all(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    rig.audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_obligation_merge(_row(rig), _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "10.1.2.3" not in str(outcome)
    assert outcome["error"]


def test_audit_persistence_failure_also_blocks_the_write(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    rig.audit.raise_on_outcome["applied"] = AuditPersistenceError("insert failed: relation x")

    outcome = execute_obligation_merge(_row(rig), _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert "relation" not in str(outcome)


def test_a_graph_failure_records_a_failed_row_with_the_same_approval_id_and_a_generic_error(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.store.fail_appends(error=GraphWriteRejectedError("host=10.1.2.3 port=6379 refused"))

    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))

    assert [r.outcome for r in rig.audit.rows] == ["applied", "failed"]
    failed = rig.audit.rows[1]
    assert failed.details["approval_id"] == "approval-1"
    assert failed.details["reason_code"] == "graph_write_failed"
    text = str(outcome)
    assert "10.1.2.3" not in text
    assert "6379" not in text
    assert "error" in outcome
    assert outcome.get("merged") is not True


def test_a_guard_miss_records_a_failed_row_and_reports_a_stale_graph(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.read.flip_before(_NODE_READ, 2, lambda: rig.seed_edge(_NEW_REQUIRES, legacy=False))

    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))

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

    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))

    assert "error" in outcome
    assert [r.outcome for r in rig.audit.rows] == ["applied"]


def test_graph_drift_since_the_preview_is_rejected_before_any_audit_or_write(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    rig.seed_edge(_NEW_REQUIRES, legacy=False)

    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "changed" in str(outcome["error"])


def test_a_pair_that_now_spans_two_roles_is_rejected_at_execution(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    for key in [
        k for k in rig.graph.edges if k[:5] == ("HAS", "Role", ROLE_ID, "Obligation", OBL_ABSORBED)
    ]:
        del rig.graph.edges[key]
    rig.seed_endpoint("Role", "role_2", name="Importer")
    rig.seed_edge(("HAS", "Role", "role_2", "Obligation", OBL_ABSORBED), legacy=False)

    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "different roles" in str(outcome["error"])


def test_a_deleted_side_at_execution_is_rejected(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    del rig.graph.nodes[("Obligation", OBL_ABSORBED)]

    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))

    assert rig.logged_groups == []
    assert "does not exist" in str(outcome["error"])


def test_a_revoked_compliance_officer_cannot_execute(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_obligation_merge(_row(rig), _CONFIG, _deps(rig, _roles(officer=False)))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "error" in outcome


def test_an_authorization_store_outage_fails_closed(cleanup_rig: CleanupRig) -> None:
    rig = _world(cleanup_rig)

    outcome = execute_obligation_merge(_row(rig), _CONFIG, _deps(rig, RaisingAccessRoleStore()))

    assert rig.logged_groups == []
    assert rig.audit.rows == []
    assert "error" in outcome


@pytest.mark.parametrize(
    "bad_args",
    [{"survivor_id": 1}, {"absorbed_id": None}, {"state_digest": 3}],
)
def test_malformed_normalized_args_are_a_generic_error_with_no_work_done(
    cleanup_rig: CleanupRig, bad_args: dict[str, object]
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)
    row = replace(row, normalized_args={**row.normalized_args, **bad_args})

    outcome = execute_obligation_merge(row, _CONFIG, _deps(rig))

    assert "error" in outcome
    assert rig.logged_groups == []
    assert rig.audit.rows == []


def test_the_effect_verifier_needs_the_absorbed_node_gone_and_the_marker_present(
    cleanup_rig: CleanupRig,
) -> None:
    rig = _world(cleanup_rig)
    row = _row(rig)

    assert verify_obligation_merge_effect(row, rig.read) is False  # no marker, node present
    rig.seed_endpoint("MergedObligation", OBL_ABSORBED, merged_into=OBL_SURVIVOR)
    assert verify_obligation_merge_effect(row, rig.read) is False  # marker, node present
    del rig.graph.nodes[("MergedObligation", OBL_ABSORBED)]
    del rig.graph.nodes[("Obligation", OBL_ABSORBED)]
    assert verify_obligation_merge_effect(row, rig.read) is False  # no marker, node gone
    rig.seed_endpoint("MergedObligation", OBL_ABSORBED, merged_into=OBL_SURVIVOR)
    assert verify_obligation_merge_effect(row, rig.read) is True  # marker, node gone
