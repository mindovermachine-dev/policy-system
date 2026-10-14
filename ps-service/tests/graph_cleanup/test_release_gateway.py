"""`release-capability-governance` writes through the Graph Write Gateway.

One approved release is one gateway group: a `DeleteEdge` of the `GOVERNED_BY` edge, linked to the
`applied` audit row, guarded by the log position read BEFORE the guard reads
(guard, group, failure matrix).
The guard is the old statement's `WHERE`, now evaluated in Python on a fresh read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from authz._fakes import FakeAccessRoleStore

from graph_cleanup._fakes import SURVIVOR
from graph_cleanup._gateway_fakes import POLICY, POLICY_SYSTEM, CleanupRig
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
from ps_service.graph_cleanup.executors import TOOL_RELEASE_GOVERNANCE, execute_release_governance
from ps_service.graph_cleanup.graph_reader import read_release_state
from ps_service.graph_cleanup.group_builders import (
    build_release_group,
    release_guard_holds,
)
from ps_service.graph_cleanup.release_planner import plan_release_governance
from ps_service.graph_gateway.errors import (
    GraphUnavailableError,
    GraphWriteRejectedError,
)
from ps_service.graph_gateway.models import DeleteEdge, ExpectedPosition, NodeRef
from ps_service.logging import configure
from ps_service.passkey_signing.models import PendingApprovalRow

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.graph_cleanup.models import ReleaseState

_OWNER = ("owner", "https://issuer.example.com/")
_OFFICER = ("officer", "https://issuer.example.com/")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    is_local_test_bypass_active=False,
)
_STATE_READ = "c.name"
"""A distinctive part of the first read of the state: its first run is the replan, the second
the guard."""
_EDGE_IDENTITY = f"GOVERNED_BY:{SURVIVOR}->{POLICY}"


@pytest.fixture(autouse=True)
def logging_configured() -> None:
    configure()


def _roles() -> FakeAccessRoleStore:
    store = FakeAccessRoleStore(expected_owner=_OWNER)
    store.bootstrap_first_owner(_OWNER)
    store.grant(actor=_OWNER, target=_OFFICER, access_role=AccessRole.COMPLIANCE_OFFICER)
    return store


def _row(rig: CleanupRig) -> PendingApprovalRow:
    state = read_release_state(rig.read, capability_id=SURVIVOR)
    now = datetime.now(UTC)
    return PendingApprovalRow(
        id="approval-1",
        code_hash=b"h",
        tool_name=TOOL_RELEASE_GOVERNANCE,
        normalized_args={
            "capability_id": SURVIVOR,
            "policy_id": POLICY,
            "state_digest": plan_release_governance(state).state_digest,
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


def _run(
    rig: CleanupRig,
    dependencies: GraphCleanupDependencies | None = None,
    *,
    row: PendingApprovalRow | None = None,
) -> dict[str, object]:
    """Execute the approval; build it first (`row`) when a test arms a read hook, as the
    approval's own preview read would count as the first read of the hooked statement.
    """
    return execute_release_governance(
        row or _row(rig), _CONFIG, dependencies or rig.dependencies(_roles())
    )


def _drop_governing_edge(rig: CleanupRig) -> None:
    for key in [
        k
        for k in rig.graph.edges
        if k[:5] == ("GOVERNED_BY", "Capability", SURVIVOR, "Policy", POLICY)
    ]:
        del rig.graph.edges[key]


def _rows(rig: CleanupRig) -> list[tuple[str, str]]:
    return [(r.action, r.outcome) for r in rig.audit.rows]


# -- the group ------------------------------------------------------------------------------------


def test_one_release_is_exactly_one_group_deleting_the_governed_by_edge_by_identity(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()
    position = cleanup_rig.store.last_position(POLICY_SYSTEM)

    outcome = _run(cleanup_rig)

    assert outcome == {"capability_id": SURVIVOR, "policy_id": POLICY, "released": True}
    [group] = cleanup_rig.hooked.groups
    assert group.graph == POLICY_SYSTEM
    assert group.primitives == (
        DeleteEdge(
            type="GOVERNED_BY",
            identity=_EDGE_IDENTITY,
            source=NodeRef(label="Capability", id=SURVIVOR),
            target=NodeRef(label="Policy", id=POLICY),
        ),
    )
    assert group.preconditions == (ExpectedPosition(position=position),)


def test_the_groups_audit_event_id_is_the_applied_rows_id(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_release_world()

    _run(cleanup_rig)

    [event_id] = cleanup_rig.audit.event_ids
    [group] = cleanup_rig.hooked.groups
    assert group.audit_event_id == event_id
    assert _rows(cleanup_rig) == [("capability.release_governance", "applied")]
    assert cleanup_rig.order() == ["audit:applied", "log_append"]


def test_the_position_is_read_before_the_guard_reads_and_the_guard_before_the_submit(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()
    row = _row(cleanup_rig)
    calls = cleanup_rig.hooked.calls
    cleanup_rig.read.flip_before(_STATE_READ, 2, lambda: calls.append("guard_read"))

    _run(cleanup_rig, row=row)

    assert calls == [
        "is_caught_up",
        "read_log_position",
        "guard_read",
        "is_caught_up",  # the stamp needs a caught-up graph, whatever the order
        "stamp_legacy_edge_identity",
        "submit_group",
    ]


def test_the_build_is_pure_and_names_nothing_but_the_edge() -> None:
    [primitive] = build_release_group(capability_id="cap_a", policy_id="pol_b")

    assert primitive == DeleteEdge(
        type="GOVERNED_BY",
        identity="GOVERNED_BY:cap_a->pol_b",
        source=NodeRef(label="Capability", id="cap_a"),
        target=NodeRef(label="Policy", id="pol_b"),
    )


# -- the guard: the old WHERE, in Python ----------------------------------------------------------


def _state(rig: CleanupRig) -> ReleaseState:
    return read_release_state(rig.read, capability_id=SURVIVOR)


def test_the_guard_holds_for_an_active_capability_governed_by_the_named_draft_policy(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()

    assert release_guard_holds(_state(cleanup_rig), policy_id=POLICY) is True


def test_a_legacy_capability_without_a_status_counts_as_active(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_release_world()
    del cleanup_rig.graph.nodes[("Capability", SURVIVOR)]["status"]

    assert release_guard_holds(_state(cleanup_rig), policy_id=POLICY) is True


def _tombstone(rig: CleanupRig) -> None:
    rig.graph.nodes[("Capability", SURVIVOR)].update(status="merged")


def _deprecate(rig: CleanupRig) -> None:
    rig.graph.nodes[("Capability", SURVIVOR)].update(status="deprecated")


def _policy_leaves_draft(rig: CleanupRig) -> None:
    rig.graph.nodes[("Policy", POLICY)].update(status="approved")


def _forget_the_capability(rig: CleanupRig) -> None:
    del rig.graph.nodes[("Capability", SURVIVOR)]


@pytest.mark.parametrize(
    "break_it",
    [_drop_governing_edge, _tombstone, _deprecate, _policy_leaves_draft, _forget_the_capability],
    ids=["edge-gone", "tombstone", "deprecated", "policy-left-draft", "capability-gone"],
)
def test_the_guard_is_false_when_any_predicate_of_the_old_statement_fails(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    cleanup_rig.seed_release_world()
    break_it(cleanup_rig)

    assert release_guard_holds(_state(cleanup_rig), policy_id=POLICY) is False


@pytest.mark.parametrize(
    "break_it",
    [_drop_governing_edge, _tombstone, _policy_leaves_draft],
    ids=["edge-gone", "tombstone", "policy-left-draft"],
)
def test_a_guard_miss_between_the_replan_and_the_write_logs_nothing(
    cleanup_rig: CleanupRig, break_it: Callable[[CleanupRig], None]
) -> None:
    cleanup_rig.seed_release_world()
    row = _row(cleanup_rig)
    cleanup_rig.read.flip_before(_STATE_READ, 2, lambda: break_it(cleanup_rig))

    outcome = _run(cleanup_rig, row=row)

    assert "changed" in str(outcome["error"])
    assert _rows(cleanup_rig) == [
        ("capability.release_governance", "applied"),
        ("capability.release_governance", "failed"),
    ]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert cleanup_rig.logged_groups == []


def test_an_unrelated_write_between_the_position_read_and_the_append_is_refused(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = _run(cleanup_rig)

    assert "changed" in str(outcome["error"])
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_guard_missed"
    assert len(cleanup_rig.logged_groups) == 1  # only the unrelated group
    assert ("GOVERNED_BY", "Capability", SURVIVOR, "Policy", POLICY, _EDGE_IDENTITY) in (
        cleanup_rig.graph.edges
    )


# -- legacy identity-less edges -------------------------------------------------------------------


def test_a_legacy_edge_is_stamped_first_so_no_parallel_edge_results(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world(legacy=True)

    outcome = _run(cleanup_rig)

    assert outcome["released"] is True
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    [event_id] = cleanup_rig.audit.event_ids
    assert links == [event_id, event_id]  # the stamp group, then the release group
    assert [k[:5] for k in cleanup_rig.graph.edges if k[0] == "GOVERNED_BY"] == [
        ("GOVERNED_BY", "Capability", "cap_other", "Policy", POLICY)
    ]
    assert all(k[5] for k in cleanup_rig.graph.edges)  # every remaining edge now has an identity


def test_a_refused_position_may_leave_the_legacy_stamp_in_the_log_and_no_release_group(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world(legacy=True)
    cleanup_rig.hooked.before_submit(cleanup_rig.append_unrelated_group)

    outcome = _run(cleanup_rig)

    assert "changed" in str(outcome["error"])
    event_id = cleanup_rig.audit.event_ids[0]  # the applied row; the failed row has its own id
    links = [cleanup_rig.store.links[g.group_id] for g in cleanup_rig.logged_groups]
    assert links == [
        event_id,
        None,
    ]  # exactly one stamp group under the applied row; the rest is unrelated
    assert ("GOVERNED_BY", "Capability", SURVIVOR, "Policy", POLICY, _EDGE_IDENTITY) in (
        cleanup_rig.graph.edges
    )  # stamped, and the delete never ran


# -- the failure matrix ---------------------------------------------------------------------------


def _reject_the_group(rig: CleanupRig) -> None:
    def _raise() -> None:
        message = "rejected by host 10.1.2.3 port 6379"
        raise GraphWriteRejectedError(message)

    rig.hooked.before_submit(_raise)


def _log_unavailable(rig: CleanupRig) -> None:
    rig.store.fail_appends()


def _graph_unreachable(rig: CleanupRig) -> None:
    def _raise() -> None:
        raise GraphUnavailableError

    rig.hooked.before_submit(_raise)


@pytest.mark.parametrize(
    "fail",
    [_reject_the_group, _log_unavailable, _graph_unreachable],
    ids=["rejected", "log-down", "unavailable"],
)
def test_a_gateway_failure_is_a_failed_row_and_the_generic_write_failed_error(
    cleanup_rig: CleanupRig, fail: Callable[[CleanupRig], None]
) -> None:
    cleanup_rig.seed_release_world()
    fail(cleanup_rig)

    outcome = _run(cleanup_rig)

    assert outcome == {
        "error": "the release could not be completed; "
        "check its status with check-cleanup-approval before retrying"
    }
    assert _rows(cleanup_rig) == [
        ("capability.release_governance", "applied"),
        ("capability.release_governance", "failed"),
    ]
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"
    assert "10.1.2.3" not in str(cleanup_rig.audit.rows[1].details)


def test_no_installed_gateway_is_a_failed_row_and_the_generic_write_failed_error(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()

    def _none(_config: ServiceConfig) -> object:
        raise GraphWriteUnavailableError("graph writes are unavailable: no gateway")

    dependencies = GraphCleanupDependencies(
        open_single_tenant_graph=lambda _config: cleanup_rig.read,
        audit_store=lambda _config: cleanup_rig.audit,
        access_role_store=lambda _config: _roles(),
        graph_write_gateway=_none,  # pyright: ignore[reportArgumentType]
    )

    outcome = _run(cleanup_rig, dependencies)

    assert "could not be completed" in str(outcome["error"])
    assert "gateway" not in str(outcome["error"])
    assert cleanup_rig.audit.rows[1].details["reason_code"] == "graph_write_failed"


def test_a_group_committed_but_not_applied_is_a_success_with_no_failed_row(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()
    cleanup_rig.graph.fail_on_write(
        redis.exceptions.ConnectionError("Error connecting to 10.1.2.3:6379")
    )

    outcome = _run(cleanup_rig)

    assert outcome == {"capability_id": SURVIVOR, "policy_id": POLICY, "released": True}
    assert _rows(cleanup_rig) == [("capability.release_governance", "applied")]
    assert len(cleanup_rig.hooked.groups) == 1
    assert "10.1.2.3" not in str(outcome)


def test_the_next_approval_while_the_graph_is_behind_its_log_writes_nothing(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()
    cleanup_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    assert _run(cleanup_rig)["released"] is True
    groups_before = len(cleanup_rig.logged_groups)

    second = _run(cleanup_rig)

    assert second == {
        "error": "the release could not be completed; "
        "check its status with check-cleanup-approval before retrying"
    }
    assert len(cleanup_rig.logged_groups) == groups_before
