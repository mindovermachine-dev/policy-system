"""Every draft edit, and the backfill that precedes it, writes to the graph the caller names."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import (
    PolicyGraphSeeder,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
)
from ps_service.policy_lifecycle.service import (
    add_control_to_draft,
    add_standard_to_draft,
    update_control_draft,
    update_policy_draft,
    update_standard_draft,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from policy_lifecycle._gateway_fakes import LifecycleRig

_OWNER = ("alice", "https://issuer.example")
_CUSTOM_GRAPH = "custom_graph"
_POLICY = "pol_x"


def _seed_on(
    rig: LifecycleRig, graph_name: str, *, status: str | None = "draft"
) -> PolicyReadGraph:
    """Seed `pol_x` with one Standard and Control into `graph_name`; return its reader."""
    graph = rig.graphs.open(graph_name)
    PolicyGraphSeeder(graph).seed_policy(
        _POLICY,
        title="Title",
        owner=_OWNER,
        standards=(
            SeedStandard(
                "std_1", "S1", status=status, controls=(SeedControl("ctl_1", "c1", status),)
            ),
        ),
        legacy_edges=False,
    )
    return PolicyReadGraph(graph)


def _update_policy(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    update_policy_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        fields={"description": "d"},
        graph=graph,
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _add_standard(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    add_standard_to_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        title="S2",
        fields={},
        graph=graph,
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _update_standard(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    update_standard_draft(
        actor=_OWNER,
        standard_id="std_1",
        fields={"description": "d"},
        graph=graph,
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _add_control(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    add_control_to_draft(
        actor=_OWNER,
        standard_id="std_1",
        title="c2",
        fields={},
        graph=graph,
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _update_control(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    update_control_draft(
        actor=_OWNER,
        control_id="ctl_1",
        fields={"description": "d"},
        graph=graph,
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
        graph_name=graph_name,
    )


_EDITS = [
    pytest.param(_update_policy, id="update_policy"),
    pytest.param(_add_standard, id="add_standard"),
    pytest.param(_update_standard, id="update_standard"),
    pytest.param(_add_control, id="add_control"),
    pytest.param(_update_control, id="update_control"),
]


@pytest.mark.parametrize("edit", _EDITS)
def test_each_draft_edit_writes_to_the_given_graph(
    lifecycle_rig: LifecycleRig, edit: Callable[[LifecycleRig, PolicyReadGraph, str], None]
) -> None:
    graph = _seed_on(lifecycle_rig, _CUSTOM_GRAPH)

    edit(lifecycle_rig, graph, _CUSTOM_GRAPH)

    (group,) = lifecycle_rig.store.groups
    assert group.graph == _CUSTOM_GRAPH
    assert lifecycle_rig.logged_groups == 0


@pytest.mark.parametrize("edit", _EDITS[2:])
def test_the_backfill_before_a_child_edit_writes_to_the_given_graph(
    lifecycle_rig: LifecycleRig, edit: Callable[[LifecycleRig, PolicyReadGraph, str], None]
) -> None:
    graph = _seed_on(lifecycle_rig, _CUSTOM_GRAPH, status=None)

    edit(lifecycle_rig, graph, _CUSTOM_GRAPH)

    assert {group.graph for group in lifecycle_rig.store.groups} == {_CUSTOM_GRAPH}
