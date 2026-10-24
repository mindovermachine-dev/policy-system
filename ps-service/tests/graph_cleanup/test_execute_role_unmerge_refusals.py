"""Executor for a Role `unmerge-entities`: authority, staleness, failures and audit.

Every refusal made before the write records a best-effort `failed` `role.unmerge` row with a
`reason_code` and changes nothing; a guard miss or a failed write records one after the `applied`
row. The world is what the REAL `role.merge` executor left in the rig.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import pytest
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import (
    INSTRUMENT_ID,
    OBL_MOVED_1,
    OBL_OF_SURVIVOR,
    ROLE_ABSORBED,
    ROLE_SURVIVOR,
)
from graph_cleanup._merge_runs import OFFICER
from graph_cleanup._unmerge_runs import role_unmerge_row, role_unmerge_world, run_role_unmerge
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.audit.models import AuditEventRow
from ps_service.graph_cleanup.audit_actions import ObligationMergeDetails
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_gateway.errors import GraphWriteRejectedError
from ps_service.logging import configure

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_cleanup._fakes import RecordedAudit
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.authz.store import AccessRoleStore

_OWNER = ("owner", "https://issuer.example.com/")
_SURVIVOR_HAS_READ = "RETURN r.id, o.id, o.text"
"""The first run of this read is the replan, the second the write step's guard read."""
_LEAKS = ("10.1.2.3", "relation", "Traceback", "/")


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


def _unmerge_rows(rig: CleanupRig) -> list[RecordedAudit]:
    return [r for r in rig.audit.rows if r.action == "role.unmerge"]


def _only_failed_row(rig: CleanupRig) -> dict[str, object]:
    """The single `failed` `role.unmerge` row a pre-write refusal records, as its details."""
    [recorded] = _unmerge_rows(rig)
    assert recorded.resource_type == "role"
    assert recorded.resource_id == ROLE_ABSORBED
    assert recorded.outcome == "failed"
    assert (recorded.actor_subject, recorded.actor_issuer) == OFFICER
    return dict(recorded.details)


def _world(rig: CleanupRig) -> int:
    """The merge's aftermath; the number of groups already logged."""
    role_unmerge_world(rig)
    return len(rig.logged_groups)


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
    groups = _world(rig)
    row = role_unmerge_row(rig)
    before = _graph_state(rig)

    outcome = run_role_unmerge(rig, row=row, dependencies=rig.dependencies(store()))

    assert "no longer holds" in str(outcome["error"])
    assert _graph_state(rig) == before
    assert len(rig.logged_groups) == groups
    details = _only_failed_row(rig)
    assert details["reason_code"] == "actor_not_authorised"
    assert details["approval_id"] == "approval-2"
    assert details["reverses_approval_id"] == "approval-1"
    assert details["survivor_id"] == ROLE_SURVIVOR
    assert details["absorbed_id"] == ROLE_ABSORBED
    assert details["instrument_id"] == INSTRUMENT_ID
    assert "before" not in details
    assert "after" not in details
    assert "restored_obligation_ids" not in details


def test_a_refusal_whose_failed_row_cannot_be_recorded_still_returns_the_refusal(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = role_unmerge_row(rig)
    rig.audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = run_role_unmerge(rig, row=row, dependencies=rig.dependencies(_no_officer()))

    assert _unmerge_rows(rig) == []
    assert "10.1.2.3" not in str(outcome)
    assert "no longer holds" in str(outcome["error"])


# -- drift since the preview, a missed guard, a failed write change nothing ----------


def _survivor_gains_an_obligation(rig: CleanupRig) -> None:
    rig.seed_endpoint("Obligation", "obl_since", text="obl_since")
    rig.seed_has(ROLE_SURVIVOR, "obl_since")


def _marker_is_repointed(rig: CleanupRig) -> None:
    rig.graph.nodes[("MergedRole", ROLE_ABSORBED)]["merged_into"] = "role_somewhere_else"


def _moved_obligation_is_taken_by_a_third_role(rig: CleanupRig) -> None:
    rig.seed_role("role_third", name="third")
    rig.seed_has("role_third", OBL_MOVED_1)


def _absorbed_role_exists_again(rig: CleanupRig) -> None:
    rig.seed_role(ROLE_ABSORBED, name="reborn")


def test_drift_since_the_preview_is_refused_stale_with_a_failed_row_and_no_change(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = role_unmerge_row(rig)
    _survivor_gains_an_obligation(rig)
    before = _graph_state(rig)

    outcome = run_role_unmerge(rig, row=row)

    assert "changed since the preview" in str(outcome["error"])
    assert _graph_state(rig) == before
    assert len(rig.logged_groups) == groups
    details = _only_failed_row(rig)
    assert details["reason_code"] == "state_digest_mismatch"
    assert details["approval_id"] == "approval-2"


@pytest.mark.parametrize(
    "drift",
    [_marker_is_repointed, _moved_obligation_is_taken_by_a_third_role, _absorbed_role_exists_again],
    ids=["marker_repointed", "moved_obligation_taken_by_a_third_role", "absorbed_exists_again"],
)
def test_a_conflict_since_the_preview_is_refused_as_rejected_with_a_failed_row(
    cleanup_rig: CleanupRig, drift: Callable[[CleanupRig], None]
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = role_unmerge_row(rig)
    drift(rig)
    before = _graph_state(rig)

    outcome = run_role_unmerge(rig, row=row)

    assert "error" in outcome
    assert _graph_state(rig) == before
    assert len(rig.logged_groups) == groups
    assert _only_failed_row(rig)["reason_code"] == "revalidation_rejected"


def test_a_dependent_merge_made_since_the_preview_is_refused_naming_its_audit_event(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = role_unmerge_row(rig)
    dependent = AuditEventRow(
        id="evt-dependent",
        occurred_at=datetime(2026, 10, 6, tzinfo=UTC),
        actor_subject="officer",
        actor_issuer="https://issuer.example.com/",
        action="obligation.merge",
        resource_type="obligation",
        resource_id=OBL_MOVED_1,
        outcome="applied",
        details=ObligationMergeDetails(
            survivor_id="obl_other",
            absorbed_id=OBL_MOVED_1,
            role_id=ROLE_SURVIVOR,
            approval_id="ap-dependent",
        ).model_dump(mode="json"),
    )
    rig.audit.history = [dependent, *rig.audit.history]
    before = _graph_state(rig)

    outcome = run_role_unmerge(rig, row=row)

    assert "evt-dependent" in str(outcome["error"])
    assert "ap-dependent" in str(outcome["error"])
    assert _graph_state(rig) == before
    assert len(rig.logged_groups) == groups
    assert _only_failed_row(rig)["reason_code"] == "revalidation_rejected"


def _survivor_loses_a_moved_has(rig: CleanupRig) -> None:
    [key] = [k for k in rig.graph.edges if k[0] == "HAS" and k[4] == OBL_MOVED_1]
    del rig.graph.edges[key]


def _third_role_takes_a_moved_obligation(rig: CleanupRig) -> None:
    _moved_obligation_is_taken_by_a_third_role(rig)


@pytest.mark.parametrize(
    "drift", [_survivor_loses_a_moved_has, _third_role_takes_a_moved_obligation]
)
def test_a_guard_miss_records_a_failed_row_after_the_applied_row_and_leaves_the_graph_unchanged(
    cleanup_rig: CleanupRig, drift: Callable[[CleanupRig], None]
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = role_unmerge_row(rig)
    before = _graph_state(rig)
    rig.read.flip_before(_SURVIVOR_HAS_READ, 2, lambda: drift(rig))

    outcome = run_role_unmerge(rig, row=row)

    assert "changed since the preview" in str(outcome["error"])
    assert [(r.outcome, r.action) for r in rig.audit.rows[1:]] == [
        ("applied", "role.unmerge"),
        ("failed", "role.unmerge"),
    ]
    failed = rig.audit.rows[-1]
    assert failed.details["reason_code"] == "graph_guard_missed"
    assert failed.details["approval_id"] == "approval-2"
    assert len(rig.logged_groups) == groups
    assert ("MergedRole", ROLE_ABSORBED) in rig.graph.nodes
    assert ("Role", ROLE_ABSORBED) not in rig.graph.nodes
    assert before != _graph_state(rig)  # only the drift itself changed the graph


def test_a_gateway_failure_records_a_failed_row_and_the_marker_stays(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = role_unmerge_row(rig)
    before = _graph_state(rig)
    rig.store.fail_appends(error=GraphWriteRejectedError("host=10.1.2.3 port=6379 refused"))

    outcome = run_role_unmerge(rig, row=row)

    assert outcome == {
        "error": "the unmerge could not be completed; "
        "check its status with check-cleanup-approval before retrying"
    }
    assert not any(leak in str(outcome) for leak in _LEAKS)
    assert [(r.outcome, r.action) for r in rig.audit.rows[1:]] == [
        ("applied", "role.unmerge"),
        ("failed", "role.unmerge"),
    ]
    assert rig.audit.rows[-1].details["reason_code"] == "graph_write_failed"
    assert _graph_state(rig) == before
    assert len(rig.logged_groups) == groups


def test_a_refused_submit_leaves_the_graph_exactly_as_it_was(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    _world(rig)
    row = role_unmerge_row(rig)
    before = _graph_state(rig)

    def _refuse_submit() -> None:
        raise GraphWriteRejectedError("group refused")

    rig.hooked.before_submit(_refuse_submit)

    outcome = run_role_unmerge(rig, row=row)

    assert "error" in outcome
    assert rig.audit.rows[-1].details["reason_code"] == "graph_write_failed"
    assert _graph_state(rig) == before
    assert ("Role", ROLE_ABSORBED) not in rig.graph.nodes
    assert [k[4] for k in rig.graph.edges if k[0] == "HAS" and k[2] == ROLE_SURVIVOR] == sorted(
        [OBL_OF_SURVIVOR, OBL_MOVED_1, "obl_msa_moved_2"]
    )


# -- the audit row comes first, failures leak nothing ---------------------


@pytest.mark.parametrize(
    "error",
    [AuditPostgresUnavailableError("db host 10.1.2.3 down"), AuditPersistenceError("relation x")],
    ids=["audit_store_unreachable", "audit_insert_failed"],
)
def test_a_failed_applied_audit_write_means_no_graph_write_and_a_fixed_message(
    cleanup_rig: CleanupRig, error: Exception
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = role_unmerge_row(rig)
    rig.audit.raise_on_outcome["applied"] = error
    before = _graph_state(rig)
    events = list(rig.order())

    outcome = run_role_unmerge(rig, row=row)

    assert outcome == {
        "error": "the unmerge was not performed because it could not be audited; "
        "nothing was changed"
    }
    assert not any(leak in str(outcome) for leak in _LEAKS)
    assert _unmerge_rows(rig) == []
    assert rig.order() == events
    assert len(rig.logged_groups) == groups
    assert _graph_state(rig) == before


class _GraphDown:
    """A graph opener that fails the way a dead database does."""

    def __call__(self, _config: object) -> NoReturn:
        message = "connection refused to 10.1.2.3:6379 relation x"
        raise ConnectionError(message)


def test_an_unreachable_graph_at_sign_time_is_a_fixed_message_with_a_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = role_unmerge_row(rig)
    live = rig.dependencies(_roles_with_officer())
    dependencies = GraphCleanupDependencies(
        open_single_tenant_graph=_GraphDown(),
        audit_store=live.audit_store,
        access_role_store=live.access_role_store,
        graph_write_gateway=live.graph_write_gateway,
    )

    outcome = run_role_unmerge(rig, row=row, dependencies=dependencies)

    assert outcome == {
        "error": "the policy graph database could not be reached; nothing was changed"
    }
    assert not any(leak in str(outcome) for leak in _LEAKS)
    assert _only_failed_row(rig)["reason_code"] == "graph_unavailable"
    assert len(rig.logged_groups) == groups


def _roles_with_officer() -> FakeAccessRoleStore:
    from graph_cleanup._merge_runs import roles

    return roles()


def test_an_unreadable_audit_trail_at_sign_time_is_a_fixed_message_and_changes_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = role_unmerge_row(rig)
    rig.audit.query_error = AuditPostgresUnavailableError("db host 10.1.2.3 down")
    before = _graph_state(rig)

    outcome = run_role_unmerge(rig, row=row)

    assert outcome == {"error": "the audit trail could not be read; nothing was changed"}
    assert _unmerge_rows(rig) == []
    assert _graph_state(rig) == before
    assert len(rig.logged_groups) == groups


def test_an_approval_with_a_changed_kind_or_merge_is_refused_stale_with_a_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = role_unmerge_row(rig)
    tampered = replace(row, normalized_args={**row.normalized_args, "merge_approval_id": "other"})

    outcome = run_role_unmerge(rig, row=tampered)

    assert "changed since the preview" in str(outcome["error"])
    assert _only_failed_row(rig)["reason_code"] == "state_digest_mismatch"


@pytest.mark.parametrize("missing", ["merged_id", "merge_approval_id", "state_digest", "kind"])
def test_a_malformed_approval_is_refused_without_a_row(
    cleanup_rig: CleanupRig, missing: str
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = role_unmerge_row(rig)
    args = {k: v for k, v in row.normalized_args.items() if k != missing}

    outcome = run_role_unmerge(rig, row=replace(row, normalized_args=args))

    assert outcome == {"error": "this approval could not be executed; ask for a new approval"}
    assert _unmerge_rows(rig) == []
