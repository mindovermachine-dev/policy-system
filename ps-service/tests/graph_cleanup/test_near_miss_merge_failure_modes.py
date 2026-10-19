"""Failure modes of a near-miss merge: a refused gateway group and an unreachable graph.

When the gateway refuses the merge group the write raises a persistence error that is
a `CompanyMergePersistenceError` (`GraphCleanupPersistenceError` is a subclass), and nothing of the
merge happened: the loser is still active with its edges and the `PendingReview` is intact. Through
the executor (the signing step) the same refusal is the fixed write-failed outcome plus a `failed`
audit row. Signing leg: an unreachable graph at execution gives a fixed outcome with no
host, path or credential, a `failed` row and no graph change.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from authz._fakes import FakeAccessRoleStore

from graph_cleanup._fakes import ABSORBED, SURVIVOR
from ps_service.authz.models import AccessRole
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.executors import TOOL_MERGE_CAPABILITIES, execute_capability_merge
from ps_service.graph_cleanup.graph_reader import read_capability_merge_state
from ps_service.graph_cleanup.graph_submission import graph_write_target
from ps_service.graph_cleanup.group_builders import write_capability_merge
from ps_service.graph_cleanup.merge_planner import plan_capability_merge
from ps_service.graph_gateway.errors import GraphWriteRejectedError
from ps_service.logging import configure
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from graph_cleanup._gateway_fakes import CleanupRig
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies

_REVIEW = "review_aaa"
_OWNER = ("owner", "https://issuer.example.com/")
_OFFICER = ("officer", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)
_SECRETS = ("10.1.2.3", "6379", "hunter2", "/secret/path")


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _roles() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _world(rig: CleanupRig) -> PendingApprovalRow:
    rig.seed_capability_merge_world()
    rig.seed_near_miss_review(_REVIEW, incoming_id=ABSORBED, existing_id=SURVIVOR)
    state = read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=TOOL_MERGE_CAPABILITIES,
        normalized_args={
            "survivor_id": SURVIVOR,
            "absorbed_id": ABSORBED,
            "acknowledge_governance_change": False,
            "state_digest": plan_capability_merge(state).state_digest,
            "review_id": _REVIEW,
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


def _refuse_the_next_group(rig: CleanupRig) -> None:
    def _refuse() -> None:
        message = "host=10.1.2.3 port=6379 password=hunter2 refused the group"
        raise GraphWriteRejectedError(message)

    rig.hooked.before_submit(_refuse)


def _graph_state(rig: CleanupRig) -> object:
    return copy.deepcopy(dict(rig.graph.nodes)), set(rig.graph.edges)


def _assert_loser_untouched(rig: CleanupRig) -> None:
    assert rig.graph.nodes[("Capability", ABSORBED)].get("status", "active") == "active"
    assert not [key for key in rig.graph.edges if key[0] == "MERGED_INTO"]
    assert any(key[0] == "REQUIRES" and key[4] == ABSORBED for key in rig.graph.edges)
    assert ("PendingReview", _REVIEW) in rig.graph.nodes


def _assert_no_leak(text: str) -> None:
    for secret in _SECRETS:
        assert secret not in text


def test_a_persistence_error_of_the_cleanup_writer_is_a_company_merge_persistence_error() -> None:
    assert issubclass(GraphCleanupPersistenceError, CompanyMergePersistenceError)


def test_a_refused_merge_group_raises_the_persistence_error_and_changes_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    _world(rig)
    state = read_capability_merge_state(rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)
    plan = plan_capability_merge(state)
    before = _graph_state(rig)
    _refuse_the_next_group(rig)

    with pytest.raises(CompanyMergePersistenceError) as raised:
        write_capability_merge(
            rig.read,
            graph_write_target(_deps(rig), _CONFIG),
            audit_event_id="audit-1",
            survivor_id=SURVIVOR,
            absorbed_id=ABSORBED,
            expected=plan.expected,
            review_id=_REVIEW,
        )

    assert isinstance(raised.value, GraphCleanupPersistenceError)
    _assert_no_leak(str(raised.value))
    assert _graph_state(rig) == before
    _assert_loser_untouched(rig)


def _deps(rig: CleanupRig) -> GraphCleanupDependencies:
    return rig.dependencies(_roles())


def test_the_executor_turns_a_refused_group_into_the_fixed_outcome_and_a_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    row = _world(rig)
    before = _graph_state(rig)
    _refuse_the_next_group(rig)

    outcome = execute_capability_merge(row, _CONFIG, _deps(rig))

    assert "could not be completed" in str(outcome["error"])
    _assert_no_leak(str(outcome))
    assert _graph_state(rig) == before
    _assert_loser_untouched(rig)
    assert [r.outcome for r in rig.audit.rows] == ["applied", "failed"]
    failed = rig.audit.rows[1].details
    assert failed["reason_code"] == "graph_write_failed"
    assert failed["review_id"] == _REVIEW


def test_an_unreachable_graph_at_execution_gives_a_fixed_outcome_and_a_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    row = _world(rig)
    before = _graph_state(rig)

    def _unreachable(_config: object) -> object:
        message = "connection refused to 10.1.2.3:6379 password=hunter2 at /secret/path"
        raise ConnectionError(message)

    dependencies = replace(_deps(rig), open_single_tenant_graph=_unreachable)

    outcome = execute_capability_merge(row, _CONFIG, dependencies)

    assert outcome == {
        "error": "the policy graph database could not be reached; nothing was changed"
    }
    _assert_no_leak(str(outcome))
    assert _graph_state(rig) == before
    assert rig.logged_groups == []
    [recorded] = rig.audit.rows
    assert recorded.outcome == "failed"
    assert recorded.details["reason_code"] == "graph_unavailable"
    assert recorded.details["review_id"] == _REVIEW
    _assert_no_leak(str(recorded.details))


def test_an_unreachable_graph_during_the_review_read_gives_the_same_fixed_outcome(
    cleanup_rig: CleanupRig,
) -> None:
    rig = cleanup_rig
    row = _world(rig)
    before = _graph_state(rig)

    def _fail() -> None:
        message = "read timed out talking to 10.1.2.3:6379 as hunter2"
        raise ConnectionError(message)

    rig.read.flip_before("PendingReview", 1, _fail)

    outcome = execute_capability_merge(row, _CONFIG, _deps(rig))

    assert outcome == {
        "error": "the policy graph database could not be reached; nothing was changed"
    }
    _assert_no_leak(str(outcome))
    assert _graph_state(rig) == before
    [recorded] = rig.audit.rows
    assert recorded.details["reason_code"] == "graph_unavailable"
