"""Executor for `merge-capabilities` (issue #190, slice 10 sub-step c).

Decision 4 ordering, fail-closed: re-check the actor's role; re-read and re-validate against
the previewed state digest; record the `applied` audit row; only then write; a guard miss or
graph failure records a `failed` row with the same `approval_id` and returns a generic error.
AC-BI-006 (executor path), AC-BI-017 (drift), AC-BI-018, AC-BI-021, AC-BI-022.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import (
    ABSORBED,
    SURVIVOR,
    OrderedGraph,
    RecordingAuditStore,
)
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.executors import (
    TOOL_MERGE_CAPABILITIES,
    execute_capability_merge,
    verify_capability_merge_effect,
)
from ps_service.graph_cleanup.graph_reader import read_capability_merge_state
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.logging import configure
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


def _digest(graph: OrderedGraph) -> str:
    state = read_capability_merge_state(graph, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    return plan_capability_merge(state).state_digest


def _row(graph: OrderedGraph, **args: object) -> PendingApprovalRow:
    now = datetime.now(UTC)
    normalized: dict[str, object] = {
        "survivor_id": SURVIVOR,
        "absorbed_id": ABSORBED,
        "acknowledge_governance_change": False,
        "state_digest": _digest(graph),
        **args,
    }
    return PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=TOOL_MERGE_CAPABILITIES,
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


def _deps(
    graph: OrderedGraph, audit: RecordingAuditStore, roles: AccessRoleStore | None = None
) -> GraphCleanupDependencies:
    role_store = roles if roles is not None else _roles()
    return GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: graph,
        audit_store=lambda _config: audit,
        access_role_store=lambda _config: role_store,
    )


def _world() -> tuple[OrderedGraph, RecordingAuditStore]:
    events: list[str] = []
    return OrderedGraph(events=events), RecordingAuditStore(events=events)


def test_happy_path_audits_first_then_writes_once_and_reports_the_merge() -> None:
    graph, audit = _world()
    row = _row(graph)

    outcome = execute_capability_merge(row, _CONFIG, _deps(graph, audit))

    assert outcome == {"survivor_id": SURVIVOR, "absorbed_id": ABSORBED, "merged": True}
    assert audit.events == ["audit:applied", "graph_write"]
    assert len(graph.write_calls) == 1
    [recorded] = audit.rows
    assert recorded.action == "capability.merge"
    assert recorded.resource_type == "capability"
    assert recorded.resource_id == ABSORBED
    assert recorded.outcome == "applied"
    assert (recorded.actor_subject, recorded.actor_issuer) == _OFFICER
    details = recorded.details
    assert details["survivor_id"] == SURVIVOR
    assert details["absorbed_id"] == ABSORBED
    assert details["policy_case"] == 1
    assert details["acknowledged"] is False
    assert details["approval_id"] == "approval-1"
    assert details["before"] and details["after"]


def _edge_keys(snapshot: object) -> set[tuple[str, str, str]]:
    edges = cast("dict[str, list[dict[str, str]]]", snapshot)["edges"]
    return {(e["rel_type"], e["source_id"], e["target_id"]) for e in edges}


def test_the_audit_row_snapshots_are_sufficient_to_reverse_the_merge() -> None:
    graph, audit = _world()

    execute_capability_merge(_row(graph), _CONFIG, _deps(graph, audit))

    details = audit.rows[0].details
    before_edges = _edge_keys(details["before"])
    after_edges = _edge_keys(details["after"])
    assert ("REQUIRES", "obl_1", ABSORBED) in before_edges
    assert ("REQUIRES", "obl_1", SURVIVOR) in after_edges
    assert ("MERGED_INTO", ABSORBED, SURVIVOR) in after_edges


def test_audit_failure_means_no_graph_write_at_all() -> None:
    graph, audit = _world()
    audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_capability_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "10.1.2.3" not in str(outcome)
    assert outcome["error"]


def test_audit_persistence_failure_also_blocks_the_write() -> None:
    graph, audit = _world()
    audit.raise_on_outcome["applied"] = AuditPersistenceError("insert failed: relation x")

    outcome = execute_capability_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert "relation" not in str(outcome)


def test_a_graph_failure_records_a_failed_row_with_the_same_approval_id_and_a_generic_error() -> (
    None
):
    graph, audit = _world()
    graph.write_error = redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")

    outcome = execute_capability_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert [r.outcome for r in audit.rows] == ["applied", "failed"]
    failed = audit.rows[1]
    assert failed.details["approval_id"] == "approval-1"
    assert failed.details["reason_code"] == "graph_write_failed"
    text = str(outcome)
    assert "10.1.2.3" not in text
    assert "6379" not in text
    assert "merged" not in outcome or outcome.get("merged") is not True
    assert "error" in outcome


def test_a_guard_miss_records_a_failed_row_and_reports_a_stale_graph() -> None:
    graph, audit = _world()
    graph.write_rows = []

    outcome = execute_capability_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert [r.outcome for r in audit.rows] == ["applied", "failed"]
    assert audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert "changed" in str(outcome["error"])


def test_a_failed_row_that_cannot_be_recorded_does_not_mask_the_error() -> None:
    graph, audit = _world()
    graph.write_error = redis.exceptions.ConnectionError("down")
    audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("down")

    outcome = execute_capability_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert "error" in outcome
    assert [r.outcome for r in audit.rows] == ["applied"]


def test_graph_drift_since_the_preview_is_rejected_before_any_audit_or_write() -> None:
    graph, audit = _world()
    row = _row(graph)
    graph.requires.append(["obl_new", ABSORBED])

    outcome = execute_capability_merge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "changed" in str(outcome["error"])


def test_a_revoked_compliance_officer_cannot_execute() -> None:
    graph, audit = _world()

    outcome = execute_capability_merge(
        _row(graph), _CONFIG, _deps(graph, audit, _roles(officer=False))
    )

    assert graph.write_calls == []
    assert audit.rows == []
    assert "error" in outcome


def test_an_authorization_store_outage_fails_closed() -> None:
    graph, audit = _world()

    outcome = execute_capability_merge(
        _row(graph), _CONFIG, _deps(graph, audit, RaisingAccessRoleStore())
    )

    assert graph.write_calls == []
    assert audit.rows == []
    assert "error" in outcome


def test_two_governed_capabilities_are_rejected_at_execution_too() -> None:
    graph, audit = _world()
    row = _row(graph)
    graph.governors = [[ABSORBED, "pol_1", "P", "draft"], [SURVIVOR, "pol_2", "Q", "draft"]]

    outcome = execute_capability_merge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "release-capability-governance" in str(outcome["error"])


def test_a_tombstoned_side_at_execution_is_rejected() -> None:
    graph, audit = _world()
    row = _row(graph)
    graph.nodes[1][2] = "merged"

    outcome = execute_capability_merge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert "merged" in str(outcome["error"])


@pytest.mark.parametrize(
    "bad_args",
    [{"survivor_id": 1}, {"absorbed_id": None}, {"state_digest": 3}],
)
def test_malformed_normalized_args_are_a_generic_error_with_no_work_done(
    bad_args: dict[str, object],
) -> None:
    graph, audit = _world()
    row = _row(graph)
    row = replace(row, normalized_args={**row.normalized_args, **bad_args})

    outcome = execute_capability_merge(row, _CONFIG, _deps(graph, audit))

    assert "error" in outcome
    assert graph.write_calls == []
    assert audit.rows == []


def test_the_effect_verifier_sees_a_tombstone() -> None:
    graph, _audit = _world()
    row = _row(graph)

    graph.tombstone_count = 0
    assert verify_capability_merge_effect(row, graph) is False
    graph.tombstone_count = 1
    assert verify_capability_merge_effect(row, graph) is True
