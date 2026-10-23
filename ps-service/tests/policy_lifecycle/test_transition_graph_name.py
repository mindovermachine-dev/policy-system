"""Each lifecycle transition and its backfill write to the graph the caller names."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)

from policy_lifecycle._gateway_fakes import (
    POLICY_SYSTEM,
    PolicyGraphSeeder,
    PolicyReadGraph,
    SeedControl,
    SeedStandard,
    policy_manager_enforcer,
)
from ps_service.authz.models import AccessRole
from ps_service.policy_lifecycle.service import (
    approve_policy,
    create_policy_draft,
    get_policy,
    propose_policy,
    reject_policy,
    revert_policy_to_draft,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from policy_lifecycle._gateway_fakes import LifecycleRig

_OWNER = ("alice", "https://issuer.example")
_MANAGER = ("carol", "https://issuer.example")
_GRANTER = ("system-admin-tool", "https://issuer.example")
_CUSTOM_GRAPH = "custom_graph"
_POLICY = "pol_x"


def _seed_on(
    rig: LifecycleRig, graph_name: str, *, status: str, version: str | None = "1"
) -> PolicyReadGraph:
    """Seed `pol_x` with a Standard and Control at `status` into `graph_name`; return a reader."""
    graph = rig.graphs.open(graph_name)
    PolicyGraphSeeder(graph).seed_policy(
        _POLICY,
        title="Title",
        status=status,
        version=version,
        owner=_OWNER,
        standards=(
            SeedStandard(
                "std_1",
                "S1",
                status=status if version is not None else None,
                controls=(
                    SeedControl("ctl_1", "c1", status=status if version is not None else None),
                ),
            ),
        ),
        legacy_edges=False,
    )
    return PolicyReadGraph(graph)


def _enforcer() -> Callable[[tuple[str, str]], None]:
    store = FakeAccessRoleStore()
    store.grant(actor=_GRANTER, target=_MANAGER, access_role=AccessRole.POLICY_MANAGER)
    return policy_manager_enforcer(store)


def _propose(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    propose_policy(
        actor=_OWNER,
        policy_id=_POLICY,
        graph=graph,
        audit_store=rig.audit,
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _approve(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    approve_policy(
        actor=_MANAGER,
        policy_id=_POLICY,
        graph=graph,
        audit_store=rig.audit,
        enforce_policy_manager=_enforcer(),
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _reject(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    reject_policy(
        actor=_MANAGER,
        policy_id=_POLICY,
        graph=graph,
        audit_store=rig.audit,
        enforce_policy_manager=_enforcer(),
        submitter=rig.gateway,
        graph_name=graph_name,
    )


def _revert(rig: LifecycleRig, graph: PolicyReadGraph, graph_name: str) -> None:
    revert_policy_to_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        graph=graph,
        audit_store=rig.audit,
        submitter=rig.gateway,
        graph_name=graph_name,
    )


@pytest.mark.parametrize(
    ("transition", "from_status"),
    [
        pytest.param(_propose, "draft", id="propose"),
        pytest.param(_approve, "proposed", id="approve"),
        pytest.param(_reject, "proposed", id="reject"),
        pytest.param(_revert, "proposed", id="revert"),
    ],
)
def test_each_transition_cascades_on_the_given_graph(
    lifecycle_rig: LifecycleRig,
    transition: Callable[[LifecycleRig, PolicyReadGraph, str], None],
    from_status: str,
) -> None:
    graph = _seed_on(lifecycle_rig, _CUSTOM_GRAPH, status=from_status)

    transition(lifecycle_rig, graph, _CUSTOM_GRAPH)

    (group,) = lifecycle_rig.store.groups
    assert group.graph == _CUSTOM_GRAPH
    assert lifecycle_rig.logged_groups == 0


def test_get_policy_backfill_writes_to_the_given_graph(lifecycle_rig: LifecycleRig) -> None:
    graph = _seed_on(lifecycle_rig, _CUSTOM_GRAPH, status="draft", version=None)

    get_policy(
        policy_id=_POLICY,
        graph=graph,
        submitter_source=lambda: lifecycle_rig.gateway,
        graph_name=_CUSTOM_GRAPH,
    )

    (group,) = lifecycle_rig.store.groups
    assert group.graph == _CUSTOM_GRAPH


def test_a_fork_backfills_the_prior_tree_on_the_given_graph(lifecycle_rig: LifecycleRig) -> None:
    graph = _seed_on(lifecycle_rig, _CUSTOM_GRAPH, status="approved", version=None)

    create_policy_draft(
        actor=_OWNER,
        title="Title",
        supersedes_policy_id=_POLICY,
        graph=graph,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
        graph_name=_CUSTOM_GRAPH,
    )

    assert {group.graph for group in lifecycle_rig.store.groups} == {_CUSTOM_GRAPH}
    assert POLICY_SYSTEM not in {group.graph for group in lifecycle_rig.store.groups}
