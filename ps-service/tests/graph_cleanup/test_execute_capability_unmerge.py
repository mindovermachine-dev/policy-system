"""Executor for capability `unmerge` (issue #190, slice 15 c; AC-BI-017/018/019/020/021/022).

Audit-first and fail-closed like the merges: re-check the role, re-locate the merge in the
audit trail, re-read and compare the state digest, record `applied`, run the guarded writer,
record `failed` (same approval id) on a guard miss or driver error.
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
    RecordingAuditStore,
    ScriptedUnmergeGraph,
    capability_merge_history,
)
from ps_service.audit.errors import AuditPersistenceError, AuditPostgresUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.executors import (
    TOOL_UNMERGE,
    execute_unmerge,
    verify_unmerge_effect,
)
from ps_service.graph_cleanup.unmerge import plan_unmerge
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


def _world() -> tuple[ScriptedUnmergeGraph, RecordingAuditStore]:
    events: list[str] = []
    return ScriptedUnmergeGraph(events=events), RecordingAuditStore(
        events=events, history=capability_merge_history()
    )


def _row(
    graph: ScriptedUnmergeGraph, audit: RecordingAuditStore, **args: object
) -> PendingApprovalRow:
    plan = plan_unmerge(graph, audit, merged_id=ABSORBED)
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-2",
        code_hash=b"h",
        tool_name=TOOL_UNMERGE,
        normalized_args={
            "merged_id": ABSORBED,
            "merge_approval_id": "merge-approval-1",
            "kind": "capability",
            "state_digest": plan.state_digest,
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
    graph: ScriptedUnmergeGraph,
    audit: RecordingAuditStore,
    roles: AccessRoleStore | None = None,
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
    assert TOOL_UNMERGE == "unmerge"
    assert resolve_approval_executor(TOOL_UNMERGE) is not None
    assert resolve_effect_verifier(TOOL_UNMERGE) is verify_unmerge_effect


def test_happy_path_audits_first_then_runs_the_guarded_write_once() -> None:
    graph, audit = _world()

    outcome = execute_unmerge(_row(graph, audit), _CONFIG, _deps(graph, audit))

    assert outcome == {
        "merged_id": ABSORBED,
        "survivor_id": SURVIVOR,
        "unmerged": True,
        "survivor_added_edges": [],
    }
    assert audit.events == ["audit:applied", "graph_write"]
    [(_text, params)] = graph.write_calls
    assert params is not None
    assert params["absorbed_id"] == ABSORBED
    assert params["survivor_id"] == SURVIVOR
    assert params["restore_requires_ids"] == ["obl_1", "obl_2"]
    assert params["remove_requires_ids"] == ["obl_1"]
    assert params["remove_covers_ids"] == ["pa_1"]


def test_the_audit_row_reverses_the_merge_and_lists_restored_and_survivor_added_edges() -> None:
    graph, audit = _world()
    graph.requires = [*graph.requires, ["obl_new", SURVIVOR]]

    outcome = execute_unmerge(_row(graph, audit), _CONFIG, _deps(graph, audit))

    [recorded] = audit.rows
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


def test_audit_failure_means_no_graph_write_at_all() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    audit.raise_on_outcome["applied"] = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "10.1.2.3" not in str(outcome)
    assert outcome["error"]


def test_audit_persistence_failure_also_blocks_the_write() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    audit.raise_on_outcome["applied"] = AuditPersistenceError("insert failed: relation x")

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert "relation" not in str(outcome)


def test_a_graph_failure_records_a_failed_row_with_the_same_approval_id_and_no_detail() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    graph.write_error = redis.exceptions.ConnectionError("host=10.1.2.3 port=6379 refused")

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert [r.outcome for r in audit.rows] == ["applied", "failed"]
    failed = audit.rows[1]
    assert failed.details["approval_id"] == "approval-2"
    assert failed.details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(outcome)
    assert "unmerged" not in outcome


def test_a_guard_miss_records_a_failed_row_and_reports_a_stale_graph() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    graph.write_rows = []

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert [r.outcome for r in audit.rows] == ["applied", "failed"]
    assert audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert "changed" in str(outcome["error"])


def test_a_failed_row_that_cannot_be_recorded_does_not_mask_the_error() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    graph.write_error = redis.exceptions.ConnectionError("down")
    audit.raise_on_outcome["failed"] = AuditPostgresUnavailableError("down")

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert "error" in outcome
    assert [r.outcome for r in audit.rows] == ["applied"]


def test_state_drift_since_the_preview_is_stale_with_no_audit_row_and_no_write() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    graph.requires = [*graph.requires, ["obl_new", SURVIVOR]]

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "changed" in str(outcome["error"])


def test_an_approval_presented_for_a_different_merge_is_rejected() -> None:
    graph, audit = _world()

    outcome = execute_unmerge(
        _row(graph, audit, merge_approval_id="some-other-merge"), _CONFIG, _deps(graph, audit)
    )

    assert graph.write_calls == []
    assert audit.rows == []
    assert "changed" in str(outcome["error"])


def test_an_approval_presented_for_a_different_id_is_rejected() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    row = replace(row, normalized_args={**row.normalized_args, "merged_id": "cap_elsewhere"})

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "error" in outcome


def test_a_conflict_that_arose_since_the_preview_is_rejected_with_its_explanation() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    graph.nodes[0][2] = "merged"
    graph.redirects = [[ABSORBED, SURVIVOR], [SURVIVOR, "cap_winner"]]

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "cap_winner" in str(outcome["error"])


def test_an_unreadable_audit_trail_is_a_generic_error_with_nothing_changed() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    audit.query_error = AuditPostgresUnavailableError("db host 10.1.2.3 down")

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert "10.1.2.3" not in str(outcome)
    assert "nothing was changed" in str(outcome["error"])


def test_an_unreachable_graph_is_a_generic_error_with_nothing_changed() -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    graph.read_error = redis.exceptions.ConnectionError("host=10.1.2.3")

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "10.1.2.3" not in str(outcome)


def test_a_revoked_compliance_officer_cannot_execute() -> None:
    graph, audit = _world()
    row = _row(graph, audit)

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit, _roles(officer=False)))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "error" in outcome


def test_an_authorization_store_outage_fails_closed() -> None:
    graph, audit = _world()
    row = _row(graph, audit)

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit, RaisingAccessRoleStore()))

    assert graph.write_calls == []
    assert audit.rows == []
    assert "error" in outcome


@pytest.mark.parametrize(
    "bad_args",
    [{"merged_id": 1}, {"merge_approval_id": None}, {"state_digest": 3}, {"kind": "bogus"}],
)
def test_malformed_normalized_args_are_a_generic_error_with_no_work_done(
    bad_args: dict[str, object],
) -> None:
    graph, audit = _world()
    row = _row(graph, audit)
    row = replace(row, normalized_args={**row.normalized_args, **bad_args})

    outcome = execute_unmerge(row, _CONFIG, _deps(graph, audit))

    assert "error" in outcome
    assert graph.write_calls == []
    assert audit.rows == []


def test_the_effect_verifier_needs_the_tombstone_to_be_gone() -> None:
    graph, audit = _world()
    row = _row(graph, audit)

    graph.unmerged_row = [1, 1]
    assert verify_unmerge_effect(row, graph) is False
    graph.unmerged_row = [1, 0]
    assert verify_unmerge_effect(row, graph) is True
