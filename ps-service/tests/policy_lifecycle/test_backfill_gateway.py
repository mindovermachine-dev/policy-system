"""The governance backfill is a read-resolve-write through the gateway.

`get_policy` (and every lifecycle read that gates on `status`) first fills what an older graph
lacks: a Policy's `version` and a Standard's or Control's `status`. It reads what is NULL, then
writes ONE group of `MergeProperty` for exactly the nodes that need it, with no audit link (a
backfill has no audit row). A tree with nothing to fill logs nothing at all, not even the
legacy-edge stamp. The gateway is only needed when there is something to write: a read of
an already-backfilled tree works with no gateway installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from policy_lifecycle._gateway_fakes import SeedControl, SeedStandard
from ps_service.api.errors import GraphWriteUnavailableError
from ps_service.policy_lifecycle.errors import (
    PolicyLifecycleGraphUnavailableError,
    PolicyNotFoundError,
)
from ps_service.policy_lifecycle.service import PolicyView, get_policy

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig
    from ps_service.graph_gateway.models import GraphLogGroup
    from ps_service.policy_lifecycle.graph_submission import LifecycleSubmitter

_OWNER = ("alice", "https://issuer.example")
_POLICY = "pol_x"


def _legacy_tree(rig: LifecycleRig, *, legacy_edges: bool = False, status: str = "draft") -> None:
    """A legacy tree: no Policy `version`, no Standard or Control `status`."""
    rig.seed_policy(
        _POLICY,
        status=status,
        version=None,
        owner=_OWNER,
        standards=(
            SeedStandard(
                "std_1",
                "S1",
                status=None,
                controls=(
                    SeedControl("ctl_1", "c1", status=None),
                    SeedControl("ctl_2", "c2", status=None),
                ),
            ),
            SeedStandard("std_2", "S2", status=None),
        ),
        legacy_edges=legacy_edges,
    )


def _read(rig: LifecycleRig, *, source: object | None = None) -> PolicyView:
    def _gateway() -> LifecycleSubmitter:
        return rig.gateway

    return get_policy(
        policy_id=_POLICY,
        graph=rig.read,
        submitter_source=_gateway if source is None else source,  # pyright: ignore[reportArgumentType]
    )


def _ops(group: GraphLogGroup) -> list[str]:
    return [str(entry.content["op"]) for entry in group.entries]


def _no_gateway() -> LifecycleSubmitter:
    raise GraphWriteUnavailableError


def test_a_legacy_tree_is_filled_in_one_group_with_no_audit_link(
    lifecycle_rig: LifecycleRig,
) -> None:
    _legacy_tree(lifecycle_rig)

    _read(lifecycle_rig)

    (group,) = lifecycle_rig.store.groups
    assert _ops(group) == ["merge_property"] * 5  # the Policy version + 2 Standards + 2 Controls
    assert lifecycle_rig.store.audit_link_of(group) is None
    assert lifecycle_rig.audit.rows == []


def test_the_read_returns_the_filled_values(lifecycle_rig: LifecycleRig) -> None:
    _legacy_tree(lifecycle_rig, status="proposed")

    view = _read(lifecycle_rig)

    assert view.version == "1"
    assert [(s.standard_id, s.status) for s in view.standards] == [
        ("std_1", "proposed"),
        ("std_2", "proposed"),
    ]
    assert [c.status for c in view.standards[0].controls] == ["proposed", "proposed"]


def test_a_control_takes_the_root_policys_status_never_its_standards(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy(
        _POLICY,
        status="approved",
        version="3",
        owner=_OWNER,
        standards=(
            SeedStandard(
                "std_1", "S1", status="draft", controls=(SeedControl("ctl_1", "c1", status=None),)
            ),
        ),
        legacy_edges=False,
    )

    _read(lifecycle_rig)

    nodes = lifecycle_rig.graph.nodes
    assert nodes[("Control", "ctl_1")]["status"] == "approved"
    assert nodes[("Standard", "std_1")]["status"] == "draft"  # already set: never overwritten
    assert nodes[("Policy", _POLICY)]["version"] == "3"  # already set: never overwritten


def test_a_second_read_changes_and_logs_nothing(lifecycle_rig: LifecycleRig) -> None:
    _legacy_tree(lifecycle_rig)
    _read(lifecycle_rig)
    position = lifecycle_rig.store.last_position("policy_system")

    _read(lifecycle_rig)

    assert lifecycle_rig.store.last_position("policy_system") == position
    assert lifecycle_rig.logged_groups == 1


def test_a_backfilled_tree_with_legacy_edges_logs_nothing_not_even_the_stamp(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Nothing to fill means no group and no stamp group."""
    lifecycle_rig.seed_policy(
        _POLICY,
        owner=_OWNER,
        standards=(SeedStandard("std_1", "S1", controls=(SeedControl("ctl_1", "c1"),)),),
        legacy_edges=True,
    )

    _read(lifecycle_rig)

    assert lifecycle_rig.store.groups == []


def test_a_legacy_tree_with_legacy_edges_logs_the_stamp_first_then_the_fill(
    lifecycle_rig: LifecycleRig,
) -> None:
    _legacy_tree(lifecycle_rig, legacy_edges=True)

    _read(lifecycle_rig)

    stamp, fill = lifecycle_rig.store.groups
    assert set(_ops(stamp)) == {"stamp_edge_identity"}
    assert set(_ops(fill)) == {"merge_property"}
    assert lifecycle_rig.store.audit_link_of(stamp) is None
    assert lifecycle_rig.store.audit_link_of(fill) is None


def test_a_policy_with_no_standards_only_needs_its_version(lifecycle_rig: LifecycleRig) -> None:
    lifecycle_rig.seed_policy(_POLICY, version=None, owner=_OWNER, legacy_edges=False)

    _read(lifecycle_rig)

    (group,) = lifecycle_rig.store.groups
    assert _ops(group) == ["merge_property"]
    assert lifecycle_rig.graph.nodes[("Policy", _POLICY)]["version"] == "1"


def test_a_standard_with_no_controls_is_filled(lifecycle_rig: LifecycleRig) -> None:
    lifecycle_rig.seed_policy(
        _POLICY,
        owner=_OWNER,
        standards=(SeedStandard("std_1", "S1", status=None),),
        legacy_edges=False,
    )

    _read(lifecycle_rig)

    assert lifecycle_rig.graph.nodes[("Standard", "std_1")]["status"] == "draft"


def test_a_missing_policy_writes_nothing_and_is_not_found(lifecycle_rig: LifecycleRig) -> None:

    with pytest.raises(PolicyNotFoundError):
        _read(lifecycle_rig)

    assert lifecycle_rig.store.groups == []


def test_a_backfilled_tree_is_read_without_asking_for_a_gateway(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The submitter is only resolved when there is something to write."""
    lifecycle_rig.seed_policy(
        _POLICY, owner=_OWNER, standards=(SeedStandard("std_1", "S1"),), legacy_edges=False
    )

    view = _read(lifecycle_rig, source=_no_gateway)

    assert view.policy_id == _POLICY


def test_a_tree_that_needs_a_fill_fails_closed_without_a_gateway(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A fill needed with no gateway fails closed."""
    _legacy_tree(lifecycle_rig)

    with pytest.raises(GraphWriteUnavailableError):
        _read(lifecycle_rig, source=_no_gateway)

    assert lifecycle_rig.store.groups == []
    assert "version" not in lifecycle_rig.graph.nodes[("Policy", _POLICY)]


def test_a_failed_fill_is_the_persistence_error_not_a_raw_driver_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A failed fill surfaces as the persistence error, not a raw `RedisError`."""
    _legacy_tree(lifecycle_rig)
    lifecycle_rig.store.fail_appends()

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _read(lifecycle_rig)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert raised.value.__cause__ is not None
    assert "version" not in lifecycle_rig.graph.nodes[("Policy", _POLICY)]


def test_a_fill_committed_but_not_applied_is_a_failure_of_the_call(
    lifecycle_rig: LifecycleRig,
) -> None:
    _legacy_tree(lifecycle_rig)
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _read(lifecycle_rig)

    assert str(raised.value).startswith("FalkorDB write failed: ")
