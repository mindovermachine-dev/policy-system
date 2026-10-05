"""Executor for `release-capability-governance` (issue #190, slice 14 c; AC-BI-013/021/022).

Audit-first and fail-closed like the merges: re-check the role, re-read and compare the state
digest, record `applied`, delete the edge, record `failed` (same approval id) on a guard miss
or driver error. The audit row carries the before/after snapshot and the governed sets.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import SURVIVOR, RecordingAuditStore, ScriptedReleaseGraph
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.executors import (
    TOOL_RELEASE_GOVERNANCE,
    execute_release_governance,
    verify_release_governance_effect,
)
from ps_service.graph_cleanup.graph_reader import read_release_state
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.logging import configure
from ps_service.passkey_signing.executors import resolve_approval_executor, resolve_effect_verifier
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from ps_service.authz.store import AccessRoleStore

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


def _world() -> tuple[ScriptedReleaseGraph, RecordingAuditStore]:
    events: list[str] = []
    return ScriptedReleaseGraph(events=events), RecordingAuditStore(events=events)


def _row(graph: ScriptedReleaseGraph, **args: object) -> PendingApprovalRow:
    state = read_release_state(graph, capability_id=SURVIVOR)
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


def _deps(
    graph: ScriptedReleaseGraph, audit: RecordingAuditStore, roles: AccessRoleStore | None = None
) -> GraphCleanupDependencies:
    role_store = roles if roles is not None else _roles()
    return GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: graph,
        audit_store=lambda _config: audit,
        access_role_store=lambda _config: role_store,
    )


def _edge_keys(snapshot: object) -> set[tuple[str, str, str]]:
    edges = cast("dict[str, list[dict[str, str]]]", snapshot)["edges"]
    return {(e["rel_type"], e["source_id"], e["target_id"]) for e in edges}


def test_the_executor_and_verifier_are_registered_under_the_tool_name() -> None:
    assert TOOL_RELEASE_GOVERNANCE == "release-capability-governance"
    assert resolve_approval_executor(TOOL_RELEASE_GOVERNANCE) is not None
    assert resolve_effect_verifier(TOOL_RELEASE_GOVERNANCE) is verify_release_governance_effect


def test_happy_path_audits_first_then_deletes_the_edge_once() -> None:
    graph, audit = _world()

    outcome = execute_release_governance(_row(graph), _CONFIG, _deps(graph, audit))

    assert outcome == {"capability_id": SURVIVOR, "policy_id": "pol_1", "released": True}
    assert audit.events == ["audit:applied", "graph_write"]
    assert len(graph.release_calls) == 1
    assert graph.release_calls[0][1] == {"capability_id": SURVIVOR, "policy_id": "pol_1"}


def test_the_audit_row_has_the_snapshot_the_governed_sets_and_the_approval_id() -> None:
    graph, audit = _world()

    execute_release_governance(_row(graph), _CONFIG, _deps(graph, audit))

    [recorded] = audit.rows
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


def test_audit_failure_means_no_graph_write_at_all() -> None:
    graph, audit = _world()
    audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_release_governance(_row(graph), _CONFIG, _deps(graph, audit))

    assert graph.release_calls == []
    assert audit.rows == []
    assert "10.1.2.3" not in str(outcome)
    assert outcome["error"]


def test_audit_persistence_failure_also_blocks_the_write() -> None:
    graph, audit = _world()
    audit.raise_on_outcome["applied"] = AuditPersistenceError("insert failed: relation x")

    outcome = execute_release_governance(_row(graph), _CONFIG, _deps(graph, audit))

    assert graph.release_calls == []
    assert "relation" not in str(outcome)


def test_a_graph_failure_records_a_failed_row_with_the_same_approval_id() -> None:
    graph, audit = _world()
    graph.release_error = redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")

    outcome = execute_release_governance(_row(graph), _CONFIG, _deps(graph, audit))

    assert [r.outcome for r in audit.rows] == ["applied", "failed"]
    failed = audit.rows[1]
    assert failed.details["approval_id"] == "approval-1"
    assert failed.details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(outcome)
    assert "released" not in outcome


def test_a_guard_miss_records_a_failed_row_and_reports_a_stale_graph() -> None:
    graph, audit = _world()
    graph.release_rows = []

    outcome = execute_release_governance(_row(graph), _CONFIG, _deps(graph, audit))

    assert [r.outcome for r in audit.rows] == ["applied", "failed"]
    assert audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert "changed" in str(outcome["error"])


def test_a_failed_row_that_cannot_be_recorded_does_not_mask_the_error() -> None:
    graph, audit = _world()
    graph.release_error = redis.exceptions.ConnectionError("down")
    audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("down")

    outcome = execute_release_governance(_row(graph), _CONFIG, _deps(graph, audit))

    assert "error" in outcome
    assert [r.outcome for r in audit.rows] == ["applied"]


def test_a_policy_that_left_draft_since_the_preview_is_rejected_before_any_audit_or_write() -> None:
    graph, audit = _world()
    row = _row(graph)
    graph.governors = [[SURVIVOR, "pol_1", "Incident Policy", "approved"]]

    outcome = execute_release_governance(row, _CONFIG, _deps(graph, audit))

    assert graph.release_calls == []
    assert audit.rows == []
    assert "policy lifecycle" in str(outcome["error"])


def test_a_released_or_regoverned_capability_since_the_preview_is_stale() -> None:
    graph, audit = _world()
    row = _row(graph)
    graph.governors = [[SURVIVOR, "pol_2", "Other Policy", "draft"]]

    outcome = execute_release_governance(row, _CONFIG, _deps(graph, audit))

    assert graph.release_calls == []
    assert audit.rows == []
    assert "changed" in str(outcome["error"])


def test_a_policy_id_that_differs_from_the_governing_policy_is_refused() -> None:
    graph, audit = _world()

    outcome = execute_release_governance(
        _row(graph, policy_id="pol_other"), _CONFIG, _deps(graph, audit)
    )

    assert graph.release_calls == []
    assert audit.rows == []
    assert "changed" in str(outcome["error"])


def test_a_revoked_compliance_officer_cannot_execute() -> None:
    graph, audit = _world()

    outcome = execute_release_governance(
        _row(graph), _CONFIG, _deps(graph, audit, _roles(officer=False))
    )

    assert graph.release_calls == []
    assert audit.rows == []
    assert "error" in outcome


def test_an_authorization_store_outage_fails_closed() -> None:
    graph, audit = _world()

    outcome = execute_release_governance(
        _row(graph), _CONFIG, _deps(graph, audit, RaisingAccessRoleStore())
    )

    assert graph.release_calls == []
    assert audit.rows == []
    assert "error" in outcome


@pytest.mark.parametrize(
    "bad_args",
    [{"capability_id": 1}, {"policy_id": None}, {"state_digest": 3}],
)
def test_malformed_normalized_args_are_a_generic_error_with_no_work_done(
    bad_args: dict[str, object],
) -> None:
    graph, audit = _world()
    row = _row(graph)
    row = replace(row, normalized_args={**row.normalized_args, **bad_args})

    outcome = execute_release_governance(row, _CONFIG, _deps(graph, audit))

    assert "error" in outcome
    assert graph.release_calls == []
    assert audit.rows == []


def test_the_effect_verifier_needs_the_edge_to_be_gone() -> None:
    graph, _audit = _world()
    row = _row(graph)

    graph.edge_count = 1
    assert verify_release_governance_effect(row, graph) is False
    graph.edge_count = 0
    assert verify_release_governance_effect(row, graph) is True
