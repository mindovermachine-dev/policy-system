"""Issue #211, S0: the lifecycle test doubles do not interpret graph writes.

Policy Lifecycle submits every write as a gateway group, so the stand-in it READS through must
refuse a write statement instead of silently applying it (a write that reached it would be a
production write path that skipped the gateway).
"""

from __future__ import annotations

import pytest

from policy_lifecycle._gateway_fakes import LifecycleRig, SeedStandard

_WRITES = [
    "MATCH (p:Policy {id: $policy_id}) SET p.status = $target_status",
    "MERGE (p:Policy {id: $policy_id}) SET p += $properties",
    "MATCH (p:Policy {id: $policy_id}) DETACH DELETE p",
]


@pytest.mark.parametrize("statement", _WRITES)
def test_the_read_double_refuses_a_write_statement(
    lifecycle_rig: LifecycleRig, statement: str
) -> None:
    lifecycle_rig.seed_policy("P")

    with pytest.raises(AssertionError, match="write"):
        lifecycle_rig.read.query(statement, {"policy_id": "P", "target_status": "x"})

    assert lifecycle_rig.graph.nodes[("Policy", "P")]["status"] == "draft"


def test_the_read_double_refuses_a_read_it_was_not_taught(lifecycle_rig: LifecycleRig) -> None:
    with pytest.raises(AssertionError, match="not a lifecycle read"):
        lifecycle_rig.read.query("MATCH (n) RETURN n", {})


def test_the_read_double_sees_what_the_gateway_rig_graph_holds(lifecycle_rig: LifecycleRig) -> None:
    lifecycle_rig.seed_policy("P", title="Access control", standards=(SeedStandard("S", "std"),))

    rows = lifecycle_rig.read.query(
        "MATCH (p:Policy {id: $policy_id}) RETURN p.id, p.title", {"policy_id": "P"}
    ).result_set

    assert rows == [["P", "Access control"]]


def test_the_audit_double_registers_each_event_id_with_the_graph_log(
    lifecycle_rig: LifecycleRig,
) -> None:
    event_id = lifecycle_rig.audit.record_standalone_with_event_id(
        actor_subject="s",
        actor_issuer="i",
        action="policy.propose",
        resource_type="policy",
        resource_id="P",
        outcome="applied",
        details={"affected_node_ids": ["P"], "from_status": "draft", "to_status": "proposed"},
    )

    assert event_id in lifecycle_rig.store.audit_rows
