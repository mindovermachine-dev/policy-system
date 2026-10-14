"""Gateway failures reach Policy Lifecycle callers as its persistence error.

Seam part: every failure `graph_submission` can see from the gateway becomes
`PolicyLifecycleGraphUnavailableError("FalkorDB write failed: ...")` with the gateway error chained,
whether the gateway refused the group, could not reach its stores, committed it without applying it,
or the graph is behind its log. The mapped text carries no host, port or traceback. The per-family
matrix (every writer family x failure) is the second half of this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import (
    POLICY_SYSTEM,
    LifecycleRig,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
)
from ps_service.authz.models import AccessRole
from ps_service.graph_gateway.errors import GraphLogPersistenceError, GraphWriteRejectedError
from ps_service.graph_gateway.models import UpsertNode
from ps_service.persistence.errors import StatePostgresConnectionError
from ps_service.policy_lifecycle.errors import PolicyLifecycleGraphUnavailableError
from ps_service.policy_lifecycle.graph_submission import (
    GraphWriteTarget,
    read_log_position,
    require_caught_up,
    submit_group,
)
from ps_service.policy_lifecycle.service import (
    StandardDraftInput,
    add_control_to_draft,
    add_standard_to_draft,
    approve_policy,
    create_policy_draft,
    get_policy,
    propose_policy,
    reject_policy,
    revert_policy_to_draft,
    update_control_draft,
    update_policy_draft,
    update_standard_draft,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_HOST = "10.1.2.3"


def _group() -> tuple[UpsertNode, ...]:
    return (UpsertNode(label="Policy", id="P", properties={"title": "P"}),)


def _target(rig: LifecycleRig) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, POLICY_SYSTEM)


def _refused(rig: LifecycleRig) -> None:
    submit_group(_target(rig), (UpsertNode(label="Nope", id="x"),), audit_event_id=None)


def _log_write_fails(rig: LifecycleRig) -> None:
    rig.store.fail_appends(error=GraphLogPersistenceError("the graph log append failed"))
    submit_group(_target(rig), _group(), audit_event_id=None)


def _postgres_down(rig: LifecycleRig) -> None:
    rig.store.fail_appends()
    submit_group(_target(rig), _group(), audit_event_id=None)


def _state_postgres_down(rig: LifecycleRig) -> None:
    rig.store.fail_appends(error=StatePostgresConnectionError("down"))
    submit_group(_target(rig), _group(), audit_event_id=None)


def _graph_unreachable(rig: LifecycleRig) -> None:
    rig.graph.fail_on_read(redis.exceptions.ConnectionError(f"Error connecting to {_HOST}:6379"))
    submit_group(_target(rig), _group(), audit_event_id=None)


def _committed_not_applied(rig: LifecycleRig) -> None:
    rig.graph.fail_on_write(redis.exceptions.ConnectionError(f"Error connecting to {_HOST}:6379"))
    submit_group(_target(rig), _group(), audit_event_id=None)


def _behind_its_log(rig: LifecycleRig) -> None:
    rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        submit_group(_target(rig), _group(), audit_event_id=None)
    require_caught_up(_target(rig))


def _position_unreadable(rig: LifecycleRig) -> None:
    rig.store.fail_reads()
    read_log_position(_target(rig))


_FAILURES: dict[str, Callable[[LifecycleRig], None]] = {
    "gateway-refused": _refused,
    "log-write-failed": _log_write_fails,
    "log-unavailable": _postgres_down,
    "state-postgres-down": _state_postgres_down,
    "graph-unreachable": _graph_unreachable,
    "committed-not-applied": _committed_not_applied,
    "graph-behind-its-log": _behind_its_log,
    "position-unreadable": _position_unreadable,
}


@pytest.mark.parametrize("failure", list(_FAILURES))
def test_every_gateway_failure_is_the_persistence_error_with_the_write_failed_prefix(
    lifecycle_rig: LifecycleRig, failure: str
) -> None:
    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _FAILURES[failure](lifecycle_rig)

    assert str(raised.value).startswith("FalkorDB write failed: ")


@pytest.mark.parametrize("failure", list(_FAILURES))
def test_the_mapped_message_carries_no_host_port_or_traceback(
    lifecycle_rig: LifecycleRig, failure: str
) -> None:
    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _FAILURES[failure](lifecycle_rig)

    text = str(raised.value).removeprefix("FalkorDB write failed: ").lower()
    assert not any(word in text for word in ("host", "port", "traceback", _HOST))


@pytest.mark.parametrize("failure", [name for name in _FAILURES if name != "graph-behind-its-log"])
def test_a_gateway_failure_is_chained_as_the_cause_where_the_gateway_raised_one(
    lifecycle_rig: LifecycleRig, failure: str
) -> None:
    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _FAILURES[failure](lifecycle_rig)

    if failure == "committed-not-applied":
        assert raised.value.__cause__ is None  # the gateway reported, it did not raise
    else:
        assert isinstance(raised.value.__cause__, Exception)


def test_a_refusal_is_chained_as_the_gateway_rejection(lifecycle_rig: LifecycleRig) -> None:
    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _refused(lifecycle_rig)

    assert isinstance(raised.value.__cause__, GraphWriteRejectedError)


def test_a_committed_not_applied_group_is_a_failure_of_the_call_and_stays_in_the_log(
    lifecycle_rig: LifecycleRig,
) -> None:
    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        _committed_not_applied(lifecycle_rig)

    assert lifecycle_rig.logged_groups == 1  # safe in the log; the gateway applies it on recovery
    assert ("Policy", "P") not in lifecycle_rig.graph.nodes


# -- S11: every writer family x every failure ------------------------------------------------------

_OWNER = ("alice", "https://issuer.example")
_MANAGER = ("carol", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")


def _roles() -> FakeAccessRoleStore:
    roles = FakeAccessRoleStore()
    roles.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)
    return roles


def _seed_tree(rig: LifecycleRig, status: str = "draft", *, backfill: bool = False) -> None:
    rig.seed_policy(
        "pol_x",
        owner=_OWNER,
        status=status,
        version=None if backfill else "1",
        standards=(
            SeedStandard(
                "std_1",
                "S1",
                status=None if backfill else status,
                controls=(SeedControl("ctl_1", "c1", status=None if backfill else status),),
            ),
        ),
        legacy_edges=True,
    )


def _seed_fork(rig: LifecycleRig) -> None:
    _seed_tree(rig, "proposed")
    rig.seed_policy("pol_old", status="approved", owner=_OWNER, legacy_edges=True)
    rig.seed_supersedes("pol_old", "pol_x")
    rig.seed_capability("cap_a")
    rig.seed_governs("cap_a", "pol_old")


class _Family:
    """One writer family: how to seed it, how to call it, and whether it leaves audit rows."""

    def __init__(
        self,
        seed: Callable[[LifecycleRig], None],
        run: Callable[[LifecycleRig], object],
        audit_outcomes: tuple[str, ...],
    ) -> None:
        self.seed = seed
        self.run = run
        self.audit_outcomes = audit_outcomes


def _read(rig: LifecycleRig) -> PolicyReadGraph:
    return PolicyReadGraph(rig.graph)


def _create(rig: LifecycleRig) -> object:
    return create_policy_draft(
        actor=_OWNER,
        title="New Policy",
        standards=(StandardDraftInput(title="S"),),
        graph=rig.read,
        audit_store=rig.audit,
        submitter=rig.gateway,
    )


def _claim(rig: LifecycleRig) -> object:
    return create_policy_draft(
        actor=_OWNER,
        title="New Policy",
        capability_ids=("cap_a",),
        graph=rig.read,
        audit_store=rig.audit,
        submitter=rig.gateway,
    )


def _transition(action: str) -> Callable[[LifecycleRig], object]:
    def _run(rig: LifecycleRig) -> object:
        if action == "propose":
            return propose_policy(
                actor=_OWNER,
                policy_id="pol_x",
                graph=rig.read,
                audit_store=rig.audit,
                submitter=rig.gateway,
            )
        if action == "revert":
            return revert_policy_to_draft(
                actor=_OWNER,
                policy_id="pol_x",
                graph=rig.read,
                audit_store=rig.audit,
                submitter=rig.gateway,
            )
        if action == "reject":
            return reject_policy(
                actor=_MANAGER,
                policy_id="pol_x",
                graph=rig.read,
                audit_store=rig.audit,
                access_role_store=_roles(),
                submitter=rig.gateway,
            )
        return approve_policy(
            actor=_MANAGER,
            policy_id="pol_x",
            graph=rig.read,
            audit_store=rig.audit,
            access_role_store=_roles(),
            submitter=rig.gateway,
        )

    return _run


def _edit(kind: str) -> Callable[[LifecycleRig], object]:
    def _run(rig: LifecycleRig) -> object:
        graph, roles = _read(rig), FakeAccessRoleStore()
        if kind == "update-policy":
            return update_policy_draft(
                actor=_OWNER,
                policy_id="pol_x",
                fields={"description": "d"},
                graph=graph,
                access_role_store=roles,
                submitter=rig.gateway,
            )
        if kind == "update-standard":
            return update_standard_draft(
                actor=_OWNER,
                standard_id="std_1",
                fields={"description": "d"},
                graph=graph,
                access_role_store=roles,
                submitter=rig.gateway,
            )
        if kind == "update-control":
            return update_control_draft(
                actor=_OWNER,
                control_id="ctl_1",
                fields={"description": "d"},
                graph=graph,
                access_role_store=roles,
                submitter=rig.gateway,
            )
        if kind == "add-standard":
            return add_standard_to_draft(
                actor=_OWNER,
                policy_id="pol_x",
                title="New",
                fields={},
                graph=graph,
                access_role_store=roles,
                submitter=rig.gateway,
            )
        return add_control_to_draft(
            actor=_OWNER,
            standard_id="std_1",
            title="New",
            control_type="manual",
            fields={},
            graph=graph,
            access_role_store=roles,
            submitter=rig.gateway,
        )

    return _run


def _backfill(rig: LifecycleRig) -> object:
    return get_policy(
        actor=_OWNER,
        policy_id="pol_x",
        graph=rig.read,
        access_role_store=FakeAccessRoleStore(),
        submitter_source=lambda: rig.gateway,
    )


def _seed_claim(rig: LifecycleRig) -> None:
    rig.seed_capability("cap_a")


_FAMILIES: dict[str, _Family] = {
    "create": _Family(lambda rig: None, _create, ("applied", "failed")),
    "claim-create": _Family(_seed_claim, _claim, ("applied", "failed")),
    "propose": _Family(
        lambda rig: _seed_tree(rig, "draft"), _transition("propose"), ("applied", "failed")
    ),
    "reject": _Family(
        lambda rig: _seed_tree(rig, "proposed"), _transition("reject"), ("applied", "failed")
    ),
    "revert": _Family(
        lambda rig: _seed_tree(rig, "proposed"), _transition("revert"), ("applied", "failed")
    ),
    "approve": _Family(
        lambda rig: _seed_tree(rig, "proposed"), _transition("approve"), ("applied", "failed")
    ),
    "approve-fork": _Family(_seed_fork, _transition("approve"), ("applied", "failed")),
    "update-policy": _Family(_seed_tree, _edit("update-policy"), ()),
    "update-standard": _Family(_seed_tree, _edit("update-standard"), ()),
    "update-control": _Family(_seed_tree, _edit("update-control"), ()),
    "add-standard": _Family(_seed_tree, _edit("add-standard"), ()),
    "add-control": _Family(_seed_tree, _edit("add-control"), ()),
    "backfill": _Family(lambda rig: _seed_tree(rig, backfill=True), _backfill, ()),
}


def _arm_log_unavailable(rig: LifecycleRig) -> None:
    rig.store.fail_appends()


def _arm_log_write_failed(rig: LifecycleRig) -> None:
    rig.store.fail_appends(error=GraphLogPersistenceError("the graph log append failed"))


def _arm_state_postgres_down(rig: LifecycleRig) -> None:
    rig.store.fail_appends(error=StatePostgresConnectionError("down"))


def _arm_committed_not_applied(rig: LifecycleRig) -> None:
    rig.graph.fail_on_write(redis.exceptions.ConnectionError(f"Error connecting to {_HOST}:6379"))


def _arm_graph_unreachable(rig: LifecycleRig) -> None:
    rig.graph.fail_on_read(redis.exceptions.ConnectionError(f"Error connecting to {_HOST}:6379"))


_ARMS: dict[str, Callable[[LifecycleRig], None]] = {
    "log-unavailable": _arm_log_unavailable,
    "log-write-failed": _arm_log_write_failed,
    "state-postgres-down": _arm_state_postgres_down,
    "committed-not-applied": _arm_committed_not_applied,
    "graph-unreachable": _arm_graph_unreachable,
}


@pytest.mark.parametrize("arm", list(_ARMS))
@pytest.mark.parametrize("family", list(_FAMILIES))
def test_a_gateway_failure_is_the_prefixed_persistence_error_in_every_writer_family(
    lifecycle_rig: LifecycleRig, family: str, arm: str
) -> None:
    """No raw driver error; the `failed` audit row where one exists today."""
    spec = _FAMILIES[family]
    spec.seed(lifecycle_rig)
    _ARMS[arm](lifecycle_rig)

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        spec.run(lifecycle_rig)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert not isinstance(raised.value, redis.exceptions.RedisError)
    assert tuple(row.outcome for row in lifecycle_rig.audit.rows) == spec.audit_outcomes


@pytest.mark.parametrize("family", list(_FAMILIES))
def test_a_graph_behind_its_log_fails_every_writer_family_with_the_prefixed_error(
    lifecycle_rig: LifecycleRig, family: str
) -> None:
    """Decision 4: a pending graph is a failure of the call, never a success."""
    spec = _FAMILIES[family]
    spec.seed(lifecycle_rig)
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        submit_group(_target(lifecycle_rig), _group(), audit_event_id=None)

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        spec.run(lifecycle_rig)

    assert str(raised.value).startswith("FalkorDB write failed: ")


@pytest.mark.parametrize(
    "family", [name for name, spec in _FAMILIES.items() if spec.audit_outcomes]
)
def test_the_applied_row_precedes_the_first_graph_log_append_in_every_audited_family(
    lifecycle_rig: LifecycleRig, family: str
) -> None:
    """The `applied` audit row is written before the group is logged."""
    spec = _FAMILIES[family]
    spec.seed(lifecycle_rig)

    spec.run(lifecycle_rig)

    events = lifecycle_rig.events
    applied = next(i for i, event in enumerate(events) if event.endswith(":applied"))
    assert not any("append" in event for event in events[:applied])
    assert any("append" in event for event in events[applied:])


@pytest.mark.parametrize(
    "family", [name for name, spec in _FAMILIES.items() if not spec.audit_outcomes]
)
def test_draft_edits_and_backfills_leave_no_audit_row_and_no_audit_link(
    lifecycle_rig: LifecycleRig, family: str
) -> None:
    """Groups of a draft edit or a backfill carry no audit link."""
    spec = _FAMILIES[family]
    spec.seed(lifecycle_rig)

    spec.run(lifecycle_rig)

    assert lifecycle_rig.audit.rows == []
    assert lifecycle_rig.logged_groups >= 1
    assert all(not group.audit_event_id for group in lifecycle_rig.store.groups)
