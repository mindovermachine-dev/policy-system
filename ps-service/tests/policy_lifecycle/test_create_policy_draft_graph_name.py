"""Creating a Policy writes its group to the graph the caller names."""

from __future__ import annotations

from typing import TYPE_CHECKING

from policy_lifecycle._gateway_fakes import POLICY_SYSTEM
from ps_service.policy_lifecycle.service import create_policy_draft

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig

_ACTOR = ("alice", "https://issuer.example")
_CUSTOM_GRAPH = "custom_graph"


def test_the_draft_is_written_to_the_given_graph(lifecycle_rig: LifecycleRig) -> None:
    create_policy_draft(
        actor=_ACTOR,
        title="Data Protection Policy",
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
        graph_name=_CUSTOM_GRAPH,
    )

    (group,) = lifecycle_rig.store.groups
    assert group.graph == _CUSTOM_GRAPH
    assert lifecycle_rig.logged_groups == 0
    assert POLICY_SYSTEM not in {g.graph for g in lifecycle_rig.store.groups}
