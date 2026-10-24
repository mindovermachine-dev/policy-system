"""Executor for `split-capability`: authority, unreachable stores, failed writes and orphans.

Every refusal before the write records a best-effort `failed` `capability.split` row with a
`reason_code` and changes nothing; a failed write records one after the `applied` row and returns a
generic message; an `applied` row that cannot be written means no write at all. A signed split
whose outcome was never recorded is settled by `check-cleanup-approval`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, NoReturn

import pytest
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import RecordingAuditStore, expire_approval
from graph_cleanup._gateway_fakes import SPLIT_A, SPLIT_POLICY, SPLIT_X
from graph_cleanup._merge_runs import CONFIG, OFFICER, OWNER, roles
from graph_cleanup._split_runs import (
    SPLIT_ACTION,
    graph_state,
    only_failed_details,
    run_capability_split,
    signed_split_approval,
    split_row,
    split_rows,
    with_args,
)
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.service import RECONCILE_GRACE, check_cleanup_approval
from ps_service.graph_gateway.errors import GraphWriteRejectedError

if TYPE_CHECKING:
    from collections.abc import Callable

    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.authz.store import AccessRoleStore
    from ps_service.graph_cleanup.service import CleanupApprovalStatus

_LEAKS = ("10.1.2.3", "6379", "relation", "refused", "Traceback", "/")
_WRITE_FAILED = {
    "error": "the split could not be completed; "
    "check its status with check-cleanup-approval before retrying"
}


def _world(rig: CleanupRig) -> int:
    """The governed split fixture; the number of groups already logged."""
    rig.seed_capability_split_world(governed=True)
    return len(rig.store.groups)


def _no_officer() -> FakeAccessRoleStore:
    """A role store where the approving officer holds nothing (the grant was revoked)."""
    store = FakeAccessRoleStore(expected_owner=OWNER)
    store.bootstrap_first_owner(OWNER)
    return store


# -- the approver must still hold ComplianceOfficer at sign time ------------------------------


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
    row = split_row(rig)
    before = graph_state(rig)

    outcome = run_capability_split(rig, row=row, dependencies=rig.dependencies(store()))

    assert "no longer holds" in str(outcome["error"])
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups
    details = only_failed_details(rig, row)
    assert details["reason_code"] == "actor_not_authorised"
    assert (details["x_id"], details["a_id"]) == (SPLIT_X, SPLIT_A)
    assert details["acknowledged"] is True
    assert details["policy_id"] == SPLIT_POLICY
    assert "before" not in details
    assert "moved_edges" not in details


def test_a_refusal_whose_failed_row_cannot_be_recorded_still_returns_the_refusal(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = split_row(rig)
    rig.audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = run_capability_split(rig, row=row, dependencies=rig.dependencies(_no_officer()))

    assert split_rows(rig) == []
    assert "10.1.2.3" not in str(outcome)
    assert "no longer holds" in str(outcome["error"])


# -- an approval the flow never creates ------------------------------------------------------


@pytest.mark.parametrize("missing", ["x_id", "a_id", "state_digest"])
def test_an_approval_without_its_signed_arguments_is_unusable_and_changes_nothing(
    cleanup_rig: CleanupRig, missing: str
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = with_args(split_row(rig), **{missing: None})
    before = graph_state(rig)

    outcome = run_capability_split(rig, row=row)

    assert outcome == {"error": "this approval could not be executed; ask for a new approval"}
    assert split_rows(rig) == []
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups


def test_a_governed_split_signed_without_the_acknowledgment_is_refused_with_a_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = with_args(split_row(rig), acknowledge_governance_change=False)
    before = graph_state(rig)

    outcome = run_capability_split(rig, row=row)

    assert "acknowledge_governance_change" in str(outcome["error"])
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups
    assert only_failed_details(rig, row)["reason_code"] == "acknowledgment_missing"


def test_a_signed_a_that_is_not_the_replanned_terminal_is_refused_stale(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = with_args(split_row(rig), a_id="cap_somewhere_else")
    before = graph_state(rig)

    outcome = run_capability_split(rig, row=row)

    assert "changed since the preview" in str(outcome["error"])
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups
    assert only_failed_details(rig, row)["reason_code"] == "state_digest_mismatch"


# -- unreachable graphs ------------------------------------------------------------------------


def _graph_down(_config: object) -> NoReturn:
    message = "connection refused to 10.1.2.3:6379 relation x"
    raise ConnectionError(message)


@pytest.mark.parametrize("opener", ["open_single_tenant_graph", "open_baseline_graphs"])
def test_an_unreachable_graph_at_sign_time_is_a_fixed_message_with_a_failed_row(
    cleanup_rig: CleanupRig, opener: str
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = split_row(rig)
    dependencies = replace(rig.dependencies(roles()), **{opener: _graph_down})
    before = graph_state(rig)

    outcome = run_capability_split(rig, row=row, dependencies=dependencies)

    assert outcome == {
        "error": "the policy graph database could not be reached; nothing was changed"
    }
    assert not any(leak in str(outcome) for leak in _LEAKS)
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups
    assert only_failed_details(rig, row)["reason_code"] == "graph_unavailable"


# -- the audit row comes first; a failed write is generic and audited --------------------------


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
    row = split_row(rig)
    rig.audit.raise_on_outcome["applied"] = error
    before = graph_state(rig)
    events = list(rig.order())

    outcome = run_capability_split(rig, row=row)

    assert outcome == {
        "error": "the split was not performed because it could not be audited; nothing was changed"
    }
    assert not any(leak in str(outcome) for leak in _LEAKS)
    assert split_rows(rig) == []
    assert rig.order() == events
    assert len(rig.store.groups) == groups
    assert graph_state(rig) == before


def test_a_gateway_failure_records_a_failed_row_and_returns_a_generic_error(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = split_row(rig)
    before = graph_state(rig)
    rig.store.fail_appends(error=GraphWriteRejectedError("host=10.1.2.3 port=6379 refused"))

    outcome = run_capability_split(rig, row=row)

    assert outcome == _WRITE_FAILED
    assert not any(leak in str(outcome) for leak in _LEAKS)
    applied, failed = split_rows(rig)
    assert (applied.outcome, failed.outcome) == ("applied", "failed")
    assert failed.details["reason_code"] == "graph_write_failed"
    assert failed.details["approval_id"] == applied.details["approval_id"] == row.id
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups


def test_a_refused_submit_leaves_the_graph_exactly_as_it_was(cleanup_rig: CleanupRig) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    row = split_row(rig)
    before = graph_state(rig)

    def _refuse_submit() -> None:
        raise GraphWriteRejectedError("group refused")

    rig.hooked.before_submit(_refuse_submit)

    outcome = run_capability_split(rig, row=row)

    assert outcome == _WRITE_FAILED
    assert split_rows(rig)[-1].details["reason_code"] == "graph_write_failed"
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups
    assert ("ResolvedCapability", SPLIT_X) in rig.graph.nodes


def test_a_failed_write_whose_failed_row_cannot_be_recorded_still_returns_the_generic_error(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    row = split_row(rig)
    rig.store.fail_appends(error=GraphWriteRejectedError("host=10.1.2.3 refused"))
    rig.audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = run_capability_split(rig, row=row)

    assert outcome == _WRITE_FAILED
    assert [r.outcome for r in split_rows(rig)] == ["applied"]


# -- an orphaned signed split is settled by check-cleanup-approval ----------------------------


def _check_orphan(
    rig: CleanupRig, *, run_first: bool
) -> tuple[CleanupApprovalStatus | None, RecordingAuditStore, str]:
    store, row = signed_split_approval(rig)
    if run_first:
        assert run_capability_split(rig, row=row)["split"] is True
    expire_approval(store, row.id, expired_for=RECONCILE_GRACE + timedelta(seconds=5))
    audit = RecordingAuditStore()
    status = check_cleanup_approval(
        pending_approval_id=row.id,
        actor=OFFICER,
        store=store,
        config=CONFIG,
        dependencies=GraphCleanupDependencies(
            open_single_tenant_graph=lambda _c: rig.read,
            audit_store=lambda _c: audit,
            graph_write_gateway=lambda _c: rig.gateway,
        ),
    )
    return status, audit, row.id


def test_an_orphan_whose_split_never_ran_gets_an_interrupted_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    groups = _world(rig)
    before = graph_state(rig)

    status, audit, approval_id = _check_orphan(rig, run_first=False)

    assert status is not None
    assert status.outcome is not None
    assert "error" in status.outcome
    [failed] = audit.rows
    assert (failed.action, failed.resource_type, failed.resource_id, failed.outcome) == (
        SPLIT_ACTION,
        "capability",
        SPLIT_X,
        "failed",
    )
    assert (failed.actor_subject, failed.actor_issuer) == OFFICER
    assert failed.details["reason_code"] == "interrupted_no_effect"
    assert failed.details["approval_id"] == approval_id
    assert graph_state(rig) == before
    assert len(rig.store.groups) == groups


def test_an_orphan_whose_split_ran_is_reconciled_as_applied_with_no_extra_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)

    status, audit, _approval_id = _check_orphan(rig, run_first=True)

    assert status is not None
    assert status.outcome == {"reconciled": "applied"}
    assert audit.rows == []
