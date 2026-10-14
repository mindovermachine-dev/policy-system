"""Creating a Policy that claims Capabilities is a guarded gateway group.

The order is the one of `internal_seed/persist.py`: the graph must be caught up, the
log position P0 is read, then the guard re-resolves every claimed Capability in Python (exists, not
a `merged` tombstone, no governor) and refuses with nothing logged -- no stamp, no group -- if the
claim no longer holds; only then are legacy edges stamped and the group submitted with
`ExpectedPosition(P0 + stamped)`. An unrelated write between the position read and the submit
trips the position guard (accepted coarser guard, Decision 2). Either way the `applied` row is
followed by a `failed` row with reason `capability_already_governed` and the existing error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from policy_lifecycle._gateway_fakes import FlippingRead, PolicyReadGraph, SeedStandard
from ps_service.policy_lifecycle.errors import (
    PolicyCapabilityAlreadyGovernedError,
    PolicyCapabilityNotFoundError,
    PolicyLifecycleGraphUnavailableError,
)
from ps_service.policy_lifecycle.service import (
    PolicyDraftResult,
    StandardDraftInput,
    create_policy_draft,
)

if TYPE_CHECKING:
    from policy_lifecycle._gateway_fakes import LifecycleRig
    from ps_service.graph_gateway.models import GraphLogGroup
    from ps_service.policy_lifecycle.graph_submission import LifecycleSubmitter

_ACTOR = ("alice", "https://issuer.example")
_TITLE = "Data Protection Policy"
_CAPS = ("cap_a", "cap_b")
_READ_GOVERNORS = "RETURN cap.id, g.id"


def _seed_caps(rig: LifecycleRig, *, legacy_edges: bool = False) -> None:
    rig.seed_capability("cap_a")
    rig.seed_capability("cap_b")
    if legacy_edges:  # identity-less edges the stamp has to fix before the claim group
        rig.seed_policy(
            "pol_other",
            owner=_ACTOR,
            standards=(SeedStandard("std_other", "Other"),),
            legacy_edges=True,
        )


def _claim(
    rig: LifecycleRig,
    *,
    graph: PolicyReadGraph | None = None,
    submitter: LifecycleSubmitter | None = None,
    standards: tuple[StandardDraftInput, ...] = (),
) -> PolicyDraftResult:
    return create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        standards=standards,
        capability_ids=_CAPS,
        graph=rig.read if graph is None else graph,
        audit_store=rig.audit,
        submitter=rig.gateway if submitter is None else submitter,
    )


def _ops(group: GraphLogGroup) -> list[str]:
    return [str(entry.content["op"]) for entry in group.entries]


def _governors(rig: LifecycleRig) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {cap: [] for cap in _CAPS}
    for edge_type, _, source, _, target, _ in rig.graph.edges:
        if edge_type == "GOVERNED_BY":
            result[source].append(target)
    return result


def test_a_claim_is_one_group_with_an_identified_governed_by_edge_per_capability(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_caps(lifecycle_rig)

    result = _claim(lifecycle_rig)

    (group,) = lifecycle_rig.store.groups
    assert _ops(group) == ["upsert_node", "upsert_edge", "upsert_edge"]
    assert {key[5] for key in lifecycle_rig.graph.edges} == {
        f"GOVERNED_BY:cap_a->{result.policy_id}",
        f"GOVERNED_BY:cap_b->{result.policy_id}",
    }
    assert _governors(lifecycle_rig) == {"cap_a": [result.policy_id], "cap_b": [result.policy_id]}
    assert result.capability_ids == _CAPS
    assert lifecycle_rig.store.audit_link_of(group) == lifecycle_rig.audit.event_ids[0]


def test_a_claim_with_children_puts_the_policy_the_edges_and_the_children_in_one_group(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_caps(lifecycle_rig)

    _claim(lifecycle_rig, standards=(StandardDraftInput(title="Encryption"),))

    (group,) = lifecycle_rig.store.groups
    assert _ops(group).count("upsert_node") == 2  # Policy + Standard
    assert _ops(group).count("upsert_edge") == 3  # two GOVERNED_BY + SUPPORTED_BY


def test_a_capability_that_does_not_exist_is_rejected_before_anything_is_written(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_capability("cap_a")

    with pytest.raises(PolicyCapabilityNotFoundError) as raised:
        _claim(lifecycle_rig)

    assert raised.value.capability_ids == ("cap_b",)
    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["rejected"]
    assert lifecycle_rig.store.groups == []


def test_a_capability_already_governed_is_rejected_before_anything_is_written(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_caps(lifecycle_rig)
    lifecycle_rig.seed_policy("pol_gov", owner=_ACTOR, legacy_edges=False)
    lifecycle_rig.seed_governs("cap_a", "pol_gov", legacy=False)

    with pytest.raises(PolicyCapabilityAlreadyGovernedError) as raised:
        _claim(lifecycle_rig)

    assert raised.value.capability_ids == ("cap_a",)
    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["rejected"]
    assert lifecycle_rig.store.groups == []


def _assert_refused_without_a_trace(rig: LifecycleRig, before_position: int) -> None:
    assert [row.outcome for row in rig.audit.rows] == ["applied", "failed"]
    failed = rig.audit.rows[1]
    assert failed.details["reason_code"] == "capability_already_governed"
    assert failed.details["capability_ids"] == []
    assert rig.store.last_position("policy_system") == before_position  # nothing logged at all
    assert not any(props.get("title") == _TITLE for props in rig.graph.nodes.values())


def test_a_capability_that_becomes_governed_after_the_pre_read_refuses_the_claim_and_logs_nothing(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_caps(lifecycle_rig, legacy_edges=True)
    lifecycle_rig.seed_policy("pol_gov", owner=_ACTOR, legacy_edges=False)
    position = lifecycle_rig.store.last_position("policy_system")
    graph = FlippingRead(
        lifecycle_rig.graph,
        lambda: lifecycle_rig.seed_governs("cap_a", "pol_gov", legacy=False),
        marker=_READ_GOVERNORS,
    )

    with pytest.raises(PolicyCapabilityAlreadyGovernedError) as raised:
        _claim(lifecycle_rig, graph=graph)

    assert raised.value.capability_ids == _CAPS
    _assert_refused_without_a_trace(lifecycle_rig, position)


def test_a_capability_that_becomes_merged_after_the_pre_read_refuses_the_claim_and_logs_nothing(
    lifecycle_rig: LifecycleRig,
) -> None:
    """A tombstone is not a governable Capability."""
    _seed_caps(lifecycle_rig, legacy_edges=True)
    position = lifecycle_rig.store.last_position("policy_system")

    def _merge() -> None:
        lifecycle_rig.graph.nodes[("Capability", "cap_b")]["status"] = "merged"

    with pytest.raises(PolicyCapabilityAlreadyGovernedError):
        _claim(
            lifecycle_rig,
            graph=FlippingRead(lifecycle_rig.graph, _merge, marker=_READ_GOVERNORS),
        )

    _assert_refused_without_a_trace(lifecycle_rig, position)


def test_an_unrelated_write_between_the_position_read_and_the_submit_refuses_the_claim(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The position guard is coarser than the in-statement guard."""
    _seed_caps(lifecycle_rig)
    lifecycle_rig.hooked.before_submit(lifecycle_rig.append_unrelated_group)

    with pytest.raises(PolicyCapabilityAlreadyGovernedError):
        _claim(lifecycle_rig, submitter=lifecycle_rig.hooked)

    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]
    assert lifecycle_rig.audit.rows[1].details["reason_code"] == "capability_already_governed"
    (injected,) = lifecycle_rig.store.groups  # only the unrelated group, none of the refused call
    assert _ops(injected) == ["upsert_node"]
    assert _governors(lifecycle_rig) == {"cap_a": [], "cap_b": []}


def test_a_stale_refusal_after_a_legacy_stamp_leaves_the_stamp_logged_and_no_claim_group(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The stamp is idempotent housekeeping (null link, no data change); it stays."""
    _seed_caps(lifecycle_rig, legacy_edges=True)
    lifecycle_rig.hooked.before_submit(lifecycle_rig.append_unrelated_group)

    with pytest.raises(PolicyCapabilityAlreadyGovernedError):
        _claim(lifecycle_rig, submitter=lifecycle_rig.hooked)

    stamp, injected = lifecycle_rig.store.groups
    assert set(_ops(stamp)) == {"stamp_edge_identity"}
    assert _ops(injected) == ["upsert_node"]
    assert lifecycle_rig.store.audit_link_of(stamp) is None
    assert _governors(lifecycle_rig) == {"cap_a": [], "cap_b": []}


def test_a_claim_on_a_graph_with_legacy_edges_stamps_them_then_submits_the_group(
    lifecycle_rig: LifecycleRig,
) -> None:
    """The expected position counts the stamp group (`P0 + stamped`), so the claim still lands."""
    _seed_caps(lifecycle_rig, legacy_edges=True)

    result = _claim(lifecycle_rig)

    stamp, claim = lifecycle_rig.store.groups
    assert set(_ops(stamp)) == {"stamp_edge_identity"}
    assert lifecycle_rig.store.audit_link_of(stamp) is None  # housekeeping carries no audit link
    assert _governors(lifecycle_rig)["cap_a"] == [result.policy_id]
    assert lifecycle_rig.store.audit_link_of(claim) == lifecycle_rig.audit.event_ids[0]


def test_a_gateway_failure_after_the_guard_records_failed_and_raises_the_persistence_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_caps(lifecycle_rig)
    lifecycle_rig.store.fail_appends()

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _claim(lifecycle_rig)

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]
    assert "reason_code" not in lifecycle_rig.audit.rows[1].details


def test_a_group_committed_but_not_applied_is_a_failure_of_the_claim(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_caps(lifecycle_rig)
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))

    with pytest.raises(PolicyLifecycleGraphUnavailableError):
        _claim(lifecycle_rig)

    assert [row.outcome for row in lifecycle_rig.audit.rows] == ["applied", "failed"]


def test_a_fork_never_claims_and_reads_no_governors(lifecycle_rig: LifecycleRig) -> None:
    """`capability_ids` is silently ignored on a fork; the plain create path runs."""
    lifecycle_rig.seed_policy("pol_prior", title=_TITLE, status="approved", owner=_ACTOR)
    _seed_caps(lifecycle_rig)

    create_policy_draft(
        actor=_ACTOR,
        title=_TITLE,
        supersedes_policy_id="pol_prior",
        capability_ids=_CAPS,
        graph=lifecycle_rig.read,
        audit_store=lifecycle_rig.audit,
        submitter=lifecycle_rig.gateway,
    )

    assert _governors(lifecycle_rig) == {"cap_a": [], "cap_b": []}
    assert not any(_READ_GOVERNORS in q for q in lifecycle_rig.read.statements)
