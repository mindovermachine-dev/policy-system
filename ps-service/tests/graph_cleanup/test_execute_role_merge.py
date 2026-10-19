"""Executor for `merge-roles`, happy path.

Audit-first ordering over the `CleanupRig` (the gateway over in-memory log and graph fakes): the
`applied` `role.merge` row is recorded, then the absorbed Role's Obligations move to the survivor,
its `DEFINES` edge goes with the deleted Role and a `MergedRole` marker is left, in one group.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import (
    ABSORBED_SOURCE_REF,
    INSTRUMENT_ID,
    OBL_MOVED_1,
    OBL_MOVED_2,
    OBL_OF_SURVIVOR,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
    SURVIVOR_SOURCE_REF,
)
from graph_cleanup._merge_runs import OFFICER, role_row, roles, run_role_merge
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.graph_cleanup.audit_actions import RoleMergeDetails
from ps_service.graph_cleanup.executors import verify_role_merge_effect
from ps_service.graph_gateway.errors import GraphWriteRejectedError
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.authz.store import AccessRoleStore

_OWNER = ("owner", "https://issuer.example.com/")
_HAS_READ = "RETURN r.id, o.id"
_OBL_NEW = "obl_msa_new"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _no_officer() -> FakeAccessRoleStore:
    """A role store where the approving officer holds nothing (the grant was revoked)."""
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    return store


def _graph_state(rig: CleanupRig) -> tuple[object, object]:
    return copy.deepcopy(rig.graph.nodes), copy.deepcopy(rig.graph.edges)


def _only_failed_row(rig: CleanupRig) -> dict[str, object]:
    """The single `failed` `role.merge` row a pre-write refusal records, as its details."""
    [recorded] = rig.audit.rows
    assert recorded.action == "role.merge"
    assert recorded.resource_type == "role"
    assert recorded.resource_id == ROLE_ABSORBED
    assert recorded.outcome == "failed"
    assert (recorded.actor_subject, recorded.actor_issuer) == OFFICER
    return dict(recorded.details)


_MOVED = [OBL_MOVED_1, OBL_MOVED_2]


def _has_sources(rig: CleanupRig, obligation_id: str) -> list[str]:
    """Ids of every Role with a `HAS` edge to the Obligation."""
    return [
        key[2]
        for key in rig.graph.edges
        if key[0] == "HAS" and key[1] == "Role" and key[4] == obligation_id
    ]


def test_happy_path_audits_first_then_writes_once_and_reports_the_merge(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()

    outcome = run_role_merge(rig)

    assert outcome == {"survivor_id": ROLE_SURVIVOR, "absorbed_id": ROLE_ABSORBED, "merged": True}
    assert rig.order() == ["audit:applied", "log_append"]
    assert len(rig.hooked.groups) == 1
    [recorded] = rig.audit.rows
    assert recorded.action == "role.merge"
    assert recorded.resource_type == "role"
    assert recorded.resource_id == ROLE_ABSORBED
    assert recorded.outcome == "applied"
    details = recorded.details
    assert details["survivor_id"] == ROLE_SURVIVOR
    assert details["absorbed_id"] == ROLE_ABSORBED
    assert details["instrument_id"] == INSTRUMENT_ID
    assert details["approval_id"] == "approval-1"
    assert details["defines_source_ref"] == ABSORBED_SOURCE_REF
    assert details["moved_obligation_ids"] == _MOVED
    assert details["survivor_has_count_after_merge"] == 3
    assert details["before"] and details["after"]


def test_the_absorbed_obligations_are_borne_by_the_survivor_with_their_ids_and_one_has_each(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()

    run_role_merge(rig)

    for obligation_id in (*_MOVED, OBL_OF_SURVIVOR):
        assert ("Obligation", obligation_id) in rig.graph.nodes
        assert _has_sources(rig, obligation_id) == [ROLE_SURVIVOR]


def test_the_absorbed_role_and_its_defines_edge_are_gone_and_the_survivors_defines_is_untouched(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()

    run_role_merge(rig)

    assert ("Role", ROLE_ABSORBED) not in rig.graph.nodes
    assert [k for k in rig.graph.edges if ROLE_ABSORBED in (k[2], k[4])] == []
    [defines] = [k for k in rig.graph.edges if k[0] == "DEFINES"]
    assert (defines[2], defines[4]) == (INSTRUMENT_ID, ROLE_SURVIVOR)
    assert rig.graph.edges[defines]["source_ref"] == SURVIVOR_SOURCE_REF


def test_a_merged_role_marker_records_the_redirect_and_is_not_a_role(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()

    run_role_merge(rig)

    assert rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]["merged_into"] == ROLE_SURVIVOR
    assert [i for (label, i) in rig.graph.nodes if label == "Role"] == [ROLE_SURVIVOR]


def test_the_effect_verifier_sees_the_merge_only_after_it_ran(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)

    before = verify_role_merge_effect(row, rig.read)
    run_role_merge(rig, row=row)
    after = verify_role_merge_effect(row, rig.read)

    assert (before, after) == (False, True)


def test_legacy_edges_without_an_identity_are_merged_too(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world(legacy=True)

    outcome = run_role_merge(rig)

    assert outcome == {"survivor_id": ROLE_SURVIVOR, "absorbed_id": ROLE_ABSORBED, "merged": True}
    for obligation_id in _MOVED:
        assert _has_sources(rig, obligation_id) == [ROLE_SURVIVOR]


# -- the approver must still hold ComplianceOfficer at sign time ---------------------


@pytest.mark.parametrize(
    "store",
    [_no_officer, RaisingAccessRoleStore],
    ids=["officer_revoked", "authorization_store_outage"],
)
def test_an_approver_who_no_longer_holds_the_role_is_refused_with_a_failed_row(
    cleanup_rig: CleanupRig, store: Callable[[], AccessRoleStore]
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)
    before = _graph_state(rig)

    outcome = run_role_merge(rig, row=row, dependencies=rig.dependencies(store()))

    assert "no longer holds" in str(outcome["error"])
    assert _graph_state(rig) == before
    assert rig.logged_groups == []
    details = _only_failed_row(rig)
    assert details["reason_code"] == "actor_not_authorised"
    assert details["approval_id"] == "approval-1"
    assert details["survivor_id"] == ROLE_SURVIVOR
    assert details["absorbed_id"] == ROLE_ABSORBED
    assert details["instrument_id"] == INSTRUMENT_ID
    assert "before" not in details
    assert "after" not in details
    assert "moved_obligation_ids" not in details


def test_a_refusal_whose_failed_row_cannot_be_recorded_still_returns_the_refusal(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    rig.audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = run_role_merge(rig, dependencies=rig.dependencies(_no_officer()))

    assert rig.audit.rows == []
    assert "10.1.2.3" not in str(outcome)
    assert "no longer holds" in str(outcome["error"])


# -- drift since the preview, a missed guard, a failed write change nothing ----------


def _add_obligation_to(rig: CleanupRig, role_id: str) -> None:
    rig.seed_endpoint("Obligation", _OBL_NEW, text=_OBL_NEW)
    rig.seed_has(role_id, _OBL_NEW)


def _drop_has(rig: CleanupRig, role_id: str, obligation_id: str) -> None:
    [key] = [
        k for k in rig.graph.edges if k[0] == "HAS" and k[2] == role_id and k[4] == obligation_id
    ]
    del rig.graph.edges[key]


def _change_defines(rig: CleanupRig, role_id: str) -> None:
    [key] = [k for k in rig.graph.edges if k[0] == "DEFINES" and k[4] == role_id]
    rig.graph.edges[key] = {**rig.graph.edges[key], "source_ref": "Art. 99"}


def _absorbed_gains_has(rig: CleanupRig) -> None:
    _add_obligation_to(rig, ROLE_ABSORBED)


def _survivor_gains_has(rig: CleanupRig) -> None:
    _add_obligation_to(rig, ROLE_SURVIVOR)


def _survivor_loses_has(rig: CleanupRig) -> None:
    _drop_has(rig, ROLE_SURVIVOR, OBL_OF_SURVIVOR)


def _absorbed_defines_changes(rig: CleanupRig) -> None:
    _change_defines(rig, ROLE_ABSORBED)


def _survivor_defines_changes(rig: CleanupRig) -> None:
    _change_defines(rig, ROLE_SURVIVOR)


@pytest.mark.parametrize(
    "drift",
    [
        _absorbed_gains_has,
        _survivor_gains_has,
        _absorbed_defines_changes,
        _survivor_defines_changes,
    ],
)
def test_drift_since_the_preview_is_refused_stale_with_a_failed_row_and_no_change(
    cleanup_rig: CleanupRig, drift: Callable[[CleanupRig], None]
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)
    drift(rig)
    before = _graph_state(rig)

    outcome = run_role_merge(rig, row=row)

    assert "changed since the preview" in str(outcome["error"])
    assert _graph_state(rig) == before
    assert rig.logged_groups == []
    details = _only_failed_row(rig)
    assert details["reason_code"] == "state_digest_mismatch"
    assert details["approval_id"] == "approval-1"


def test_a_role_deleted_since_the_preview_is_refused_as_rejected_with_a_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)
    del rig.graph.nodes[("Role", ROLE_ABSORBED)]
    before = _graph_state(rig)

    outcome = run_role_merge(rig, row=row)

    assert "does not exist" in str(outcome["error"])
    assert _graph_state(rig) == before
    assert _only_failed_row(rig)["reason_code"] == "revalidation_rejected"


@pytest.mark.parametrize("drift", [_absorbed_gains_has, _survivor_loses_has])
def test_a_guard_miss_records_a_failed_row_and_leaves_the_graph_unchanged(
    cleanup_rig: CleanupRig, drift: Callable[[CleanupRig], None]
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)
    before = _graph_state(rig)
    rig.read.flip_before(_HAS_READ, 2, lambda: drift(rig))

    outcome = run_role_merge(rig, row=row)

    assert "changed since the preview" in str(outcome["error"])
    assert [r.outcome for r in rig.audit.rows] == ["applied", "failed"]
    failed = rig.audit.rows[1]
    assert failed.action == "role.merge"
    assert failed.details["reason_code"] == "graph_guard_missed"
    assert failed.details["approval_id"] == "approval-1"
    assert rig.logged_groups == []
    assert ("Role", ROLE_ABSORBED) in rig.graph.nodes
    assert ("MergedRole", ROLE_ABSORBED) not in rig.graph.nodes
    assert before != _graph_state(rig)  # only the drift itself changed the graph


def test_a_gateway_failure_records_a_failed_row_and_the_absorbed_role_keeps_all_its_edges(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)
    before = _graph_state(rig)
    rig.store.fail_appends(error=GraphWriteRejectedError("host=10.1.2.3 port=6379 refused"))

    outcome = run_role_merge(rig, row=row)

    assert "error" in outcome
    assert "10.1.2.3" not in str(outcome)
    assert "6379" not in str(outcome)
    assert [r.outcome for r in rig.audit.rows] == ["applied", "failed"]
    failed = rig.audit.rows[1]
    assert failed.details["reason_code"] == "graph_write_failed"
    assert failed.details["approval_id"] == "approval-1"
    assert _graph_state(rig) == before
    assert rig.logged_groups == []


def test_a_refused_submit_leaves_the_absorbed_role_intact_with_all_its_edges(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)
    before = _graph_state(rig)

    def _refuse_submit() -> None:
        raise GraphWriteRejectedError("group refused")

    rig.hooked.before_submit(_refuse_submit)

    outcome = run_role_merge(rig, row=row)

    assert "error" in outcome
    assert [r.outcome for r in rig.audit.rows] == ["applied", "failed"]
    assert rig.audit.rows[1].details["reason_code"] == "graph_write_failed"
    assert _graph_state(rig) == before


# -- the audit row comes first, failures leak nothing ---------------------

_LEAKS = ("10.1.2.3", "relation", "Traceback", "/")


@pytest.mark.parametrize(
    "error",
    [AuditPostgresUnavailableError("db host 10.1.2.3 down"), AuditPersistenceError("relation x")],
    ids=["audit_store_unreachable", "audit_insert_failed"],
)
def test_a_failed_applied_audit_write_means_no_graph_write_and_a_fixed_message(
    cleanup_rig: CleanupRig, error: Exception
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)
    rig.audit.raise_on_outcome["applied"] = error
    before = _graph_state(rig)

    outcome = run_role_merge(rig, row=row)

    assert outcome == {
        "error": "the merge was not performed because it could not be audited; nothing was changed"
    }
    assert not any(leak in str(outcome) for leak in _LEAKS)
    assert rig.audit.rows == []
    assert rig.logged_groups == []
    assert rig.order() == []
    assert _graph_state(rig) == before


def test_an_unreachable_graph_at_sign_time_is_a_fixed_message_with_a_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()
    row = role_row(rig)
    deps = replace(
        rig.dependencies(roles()),
        open_single_tenant_graph=_unreachable_graph,
    )
    before = _graph_state(rig)

    outcome = run_role_merge(rig, row=row, dependencies=deps)

    assert outcome == {
        "error": "the policy graph database could not be reached; nothing was changed"
    }
    assert not any(leak in str(outcome) for leak in _LEAKS)
    assert _only_failed_row(rig)["reason_code"] == "graph_unavailable"
    assert rig.logged_groups == []
    assert _graph_state(rig) == before


def _unreachable_graph(_config: object) -> object:
    message = "connection refused to 10.1.2.3:6379 relation x"
    raise ConnectionError(message)


def test_the_applied_row_is_recorded_before_the_group_and_names_actor_pair_and_snapshots(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    rig.seed_role_merge_world()

    run_role_merge(rig)

    assert rig.order() == ["audit:applied", "log_append"]
    [recorded] = rig.audit.rows
    assert (recorded.actor_subject, recorded.actor_issuer) == OFFICER
    details = RoleMergeDetails.model_validate(recorded.details)
    assert (details.survivor_id, details.absorbed_id) == (ROLE_SURVIVOR, ROLE_ABSORBED)
    assert details.before is not None
    assert details.after is not None
    assert {(n.label, n.id) for n in details.before.nodes} == {
        ("Role", ROLE_SURVIVOR),
        ("Role", ROLE_ABSORBED),
    }
    assert {(n.label, n.id) for n in details.after.nodes} == {
        ("Role", ROLE_SURVIVOR),
        ("MergedRole", ROLE_ABSORBED),
    }
