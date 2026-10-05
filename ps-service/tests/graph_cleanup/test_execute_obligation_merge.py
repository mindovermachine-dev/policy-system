"""Executor for `merge-obligations` (issue #190, slice 11 sub-step c).

Decision 4 ordering, fail-closed: re-check the actor's role; re-read and re-validate against
the previewed state digest; record the `applied` audit row; only then write; a guard miss or
graph failure records a `failed` row with the same `approval_id` and returns a generic error.
AC-BI-007 (executor path), AC-BI-015/016 (re-validation), AC-BI-017 (drift), AC-BI-018,
AC-BI-021, AC-BI-022.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions
from authz._fakes import FakeAccessRoleStore, RaisingAccessRoleStore

from graph_cleanup._fakes import (
    OBL_ABSORBED,
    OBL_SURVIVOR,
    RecordingAuditStore,
    ScriptedObligationGraph,
)
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.executors import (
    TOOL_MERGE_OBLIGATIONS,
    execute_obligation_merge,
    verify_obligation_merge_effect,
)
from ps_service.graph_cleanup.graph_reader import read_obligation_merge_state
from ps_service.graph_cleanup.obligation_planner import plan_obligation_merge
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


def _digest(graph: ScriptedObligationGraph) -> str:
    state = read_obligation_merge_state(graph, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED)
    return plan_obligation_merge(state).state_digest


def _row(graph: ScriptedObligationGraph, **args: object) -> PendingApprovalRow:
    now = datetime.now(UTC)
    normalized: dict[str, object] = {
        "survivor_id": OBL_SURVIVOR,
        "absorbed_id": OBL_ABSORBED,
        "state_digest": _digest(graph),
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


def _deps(
    graph: ScriptedObligationGraph,
    audit: RecordingAuditStore,
    roles: AccessRoleStore | None = None,
) -> GraphCleanupDependencies:
    role_store = roles if roles is not None else _roles()
    return GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: graph,
        audit_store=lambda _config: audit,
        access_role_store=lambda _config: role_store,
    )


def _world() -> tuple[ScriptedObligationGraph, RecordingAuditStore]:
    events: list[str] = []
    return ScriptedObligationGraph(events=events), RecordingAuditStore(events=events)


def _edge_keys(snapshot: object) -> set[tuple[str, str, str]]:
    edges = cast("dict[str, list[dict[str, str]]]", snapshot)["edges"]
    return {(e["rel_type"], e["source_id"], e["target_id"]) for e in edges}


def test_happy_path_audits_first_then_writes_once_and_reports_the_merge() -> None:
    graph, audit = _world()
    row = _row(graph)

    outcome = execute_obligation_merge(row, _CONFIG, _deps(graph, audit))

    assert outcome == {"survivor_id": OBL_SURVIVOR, "absorbed_id": OBL_ABSORBED, "merged": True}
    assert audit.events == ["audit:applied", "graph_write"]
    assert len(graph.write_calls) == 1
    [recorded] = audit.rows
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


def test_the_audit_row_holds_the_absorbed_node_in_full_and_every_edge() -> None:
    graph, audit = _world()

    execute_obligation_merge(_row(graph), _CONFIG, _deps(graph, audit))

    details = audit.rows[0].details
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


def test_audit_failure_means_no_graph_write_at_all() -> None:
    graph, audit = _world()
    audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_obligation_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "10.1.2.3" not in str(outcome)
    assert outcome["error"]


def test_audit_persistence_failure_also_blocks_the_write() -> None:
    graph, audit = _world()
    audit.raise_on_outcome["applied"] = AuditPersistenceError("insert failed: relation x")

    outcome = execute_obligation_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert "relation" not in str(outcome)


def test_a_graph_failure_records_a_failed_row_with_the_same_approval_id_and_a_generic_error() -> (
    None
):
    graph, audit = _world()
    graph.write_error = redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")

    outcome = execute_obligation_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert [r.outcome for r in audit.rows] == ["applied", "failed"]
    failed = audit.rows[1]
    assert failed.details["approval_id"] == "approval-1"
    assert failed.details["reason_code"] == "graph_write_failed"
    text = str(outcome)
    assert "10.1.2.3" not in text
    assert "6379" not in text
    assert "error" in outcome
    assert outcome.get("merged") is not True


def test_a_guard_miss_records_a_failed_row_and_reports_a_stale_graph() -> None:
    graph, audit = _world()
    graph.write_rows = []

    outcome = execute_obligation_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert [r.outcome for r in audit.rows] == ["applied", "failed"]
    assert audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert "changed" in str(outcome["error"])


def test_a_failed_row_that_cannot_be_recorded_does_not_mask_the_error() -> None:
    graph, audit = _world()
    graph.write_error = redis.exceptions.ConnectionError("down")
    audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("down")

    outcome = execute_obligation_merge(_row(graph), _CONFIG, _deps(graph, audit))

    assert "error" in outcome
    assert [r.outcome for r in audit.rows] == ["applied"]


def test_graph_drift_since_the_preview_is_rejected_before_any_audit_or_write() -> None:
    graph, audit = _world()
    row = _row(graph)
    graph.requires.append([OBL_ABSORBED, "cap_new"])

    outcome = execute_obligation_merge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "changed" in str(outcome["error"])


def test_a_pair_that_now_spans_two_roles_is_rejected_at_execution() -> None:
    graph, audit = _world()
    row = _row(graph)
    graph.roles[1] = [OBL_ABSORBED, "role_2", "Importer"]

    outcome = execute_obligation_merge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "different roles" in str(outcome["error"])


def test_a_deleted_side_at_execution_is_rejected() -> None:
    graph, audit = _world()
    row = _row(graph)
    graph.nodes = graph.nodes[:1]

    outcome = execute_obligation_merge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert "does not exist" in str(outcome["error"])


def test_a_revoked_compliance_officer_cannot_execute() -> None:
    graph, audit = _world()

    outcome = execute_obligation_merge(
        _row(graph), _CONFIG, _deps(graph, audit, _roles(officer=False))
    )

    assert graph.write_calls == []
    assert audit.rows == []
    assert "error" in outcome


def test_an_authorization_store_outage_fails_closed() -> None:
    graph, audit = _world()

    outcome = execute_obligation_merge(
        _row(graph), _CONFIG, _deps(graph, audit, RaisingAccessRoleStore())
    )

    assert graph.write_calls == []
    assert audit.rows == []
    assert "error" in outcome


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

    outcome = execute_obligation_merge(row, _CONFIG, _deps(graph, audit))

    assert "error" in outcome
    assert graph.write_calls == []
    assert audit.rows == []


def test_the_effect_verifier_needs_the_absorbed_node_gone_and_the_marker_present() -> None:
    graph, _audit = _world()
    row = _row(graph)

    graph.marker_count, graph.absorbed_count = 0, 1
    assert verify_obligation_merge_effect(row, graph) is False
    graph.marker_count, graph.absorbed_count = 1, 1
    assert verify_obligation_merge_effect(row, graph) is False
    graph.marker_count, graph.absorbed_count = 0, 0
    assert verify_obligation_merge_effect(row, graph) is False
    graph.marker_count, graph.absorbed_count = 1, 0
    assert verify_obligation_merge_effect(row, graph) is True
