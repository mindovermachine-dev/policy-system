"""Draft Policy edits are one gateway group, with no audit link and no precondition (issue #211).

`update_policy_draft` reads the Policy, authorizes the edit, then submits the patch as ONE group:
a `MergeProperty` for the fields set and a `RemoveProperty` for the fields cleared. A draft edit
writes no `audit_events` row today, so its group carries a null link (AC-BI-008); a cleared
property is absent afterwards (AC-BI-007); re-applying the same patch logs nothing; a gateway
failure is the module's persistence error with the `FalkorDB write failed: ` prefix (AC-BI-011).
The Standard and Control edits and adds (S4) follow the same rules: one group, no audit link; an
add is an `UpsertNode` plus the parent's `UpsertEdge` (AC-BI-007, AC-BI-008).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from authz._fakes import (  # pyright: ignore[reportPrivateUsage]  -- `tests/authz/` is an importable package; mirrors `test_service.py`'s cross-package import convention
    FakeAccessRoleStore,
)
from pydantic import ValidationError

from policy_lifecycle._gateway_fakes import PolicyReadGraph, SeedControl, SeedStandard
from ps_service.domain_mapper.identity import control_id, standard_id
from ps_service.policy_lifecycle.errors import PolicyLifecycleGraphUnavailableError
from ps_service.policy_lifecycle.service import (
    add_control_to_draft,
    add_standard_to_draft,
    update_control_draft,
    update_policy_draft,
    update_standard_draft,
)

if TYPE_CHECKING:
    from typing import Literal

    from policy_lifecycle._gateway_fakes import LifecycleRig
    from ps_service.graph_gateway.models import GraphLogGroup

_OWNER = ("alice", "https://issuer.example")
_POLICY = "pol_x"


def _seed(rig: LifecycleRig, *, legacy_edges: bool = False, **extra: object) -> None:
    rig.seed_policy(
        _POLICY,
        owner=_OWNER,
        extra=extra,
        standards=(SeedStandard("std_1", "S"),),
        legacy_edges=legacy_edges,
    )


def _edit(rig: LifecycleRig, fields: dict[str, object]) -> None:
    update_policy_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        fields=fields,
        graph=PolicyReadGraph(rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
    )


def _ops(group: GraphLogGroup) -> list[str]:
    return [str(entry.content["op"]) for entry in group.entries]


def test_a_cleared_property_is_absent_afterwards_and_the_others_are_kept(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-007."""
    _seed(lifecycle_rig, description="old", scope_out="out", scope_in="in")

    _edit(lifecycle_rig, {"description": "new", "scope_out": None})

    node = lifecycle_rig.graph.nodes[("Policy", _POLICY)]
    assert "scope_out" not in node
    assert (node["description"], node["scope_in"]) == ("new", "in")


def test_a_patch_that_sets_and_clears_is_one_group_of_a_merge_then_a_remove(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, description="old", scope_out="out")

    _edit(lifecycle_rig, {"description": "new", "scope_out": None})

    (group,) = lifecycle_rig.store.groups
    assert _ops(group) == ["merge_property", "remove_property"]


def test_a_draft_edit_group_carries_no_audit_link(lifecycle_rig: LifecycleRig) -> None:
    """AC-BI-008: draft edits write no audit row and their groups carry no audit link."""
    _seed(lifecycle_rig, description="old")

    _edit(lifecycle_rig, {"description": "new"})

    (group,) = lifecycle_rig.store.groups
    assert lifecycle_rig.store.audit_link_of(group) is None
    assert lifecycle_rig.audit.rows == []


def test_a_patch_that_only_sets_has_no_remove_and_one_that_only_clears_has_no_merge(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, description="old", scope_out="out")

    _edit(lifecycle_rig, {"description": "new"})
    _edit(lifecycle_rig, {"scope_out": None})

    first, second = lifecycle_rig.store.groups
    assert (_ops(first), _ops(second)) == (["merge_property"], ["remove_property"])


def test_re_applying_the_same_patch_logs_nothing(lifecycle_rig: LifecycleRig) -> None:
    _seed(lifecycle_rig, description="old", scope_out="out")
    _edit(lifecycle_rig, {"description": "new", "scope_out": None})
    logged = lifecycle_rig.logged_groups
    position = lifecycle_rig.store.last_position("policy_system")

    _edit(lifecycle_rig, {"description": "new", "scope_out": None})

    assert lifecycle_rig.logged_groups == logged
    assert lifecycle_rig.store.last_position("policy_system") == position


def test_the_first_edit_of_a_graph_with_legacy_edges_logs_the_stamp_group_first_with_no_link(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, legacy_edges=True, description="old")

    _edit(lifecycle_rig, {"description": "new"})
    _edit(lifecycle_rig, {"description": "newer"})

    stamp, first, second = lifecycle_rig.store.groups
    assert [_ops(group) for group in (stamp, first, second)] == [
        ["stamp_edge_identity"],
        ["merge_property"],
        ["merge_property"],
    ]
    assert [lifecycle_rig.store.audit_link_of(g) for g in (stamp, first, second)] == [None] * 3
    assert (
        "SUPPORTED_BY",
        "Policy",
        _POLICY,
        "Standard",
        "std_1",
        "SUPPORTED_BY:pol_x->std_1",
    ) in (lifecycle_rig.graph.edges)


def test_a_value_the_gateway_cannot_store_is_refused_with_the_persistence_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Section 9 item 6: an empty list was accepted by the direct writer."""
    _seed(lifecycle_rig, description="old")

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _edit(lifecycle_rig, {"description": []})

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert "[]" not in str(raised.value)  # the offending value is never echoed
    assert isinstance(raised.value.__cause__, ValidationError)
    assert lifecycle_rig.graph.nodes[("Policy", _POLICY)]["description"] == "old"
    assert lifecycle_rig.logged_groups == 0


def test_a_gateway_failure_is_the_persistence_error_with_the_gateway_error_chained(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-011: the direct writer leaked a raw driver error from here; now it is mapped."""
    _seed(lifecycle_rig, description="old")
    lifecycle_rig.store.fail_appends()

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _edit(lifecycle_rig, {"description": "new"})

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert raised.value.__cause__ is not None
    assert lifecycle_rig.graph.nodes[("Policy", _POLICY)]["description"] == "old"


def test_a_committed_group_the_graph_could_not_apply_is_a_failure_of_the_call(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed(lifecycle_rig, description="old")
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _edit(lifecycle_rig, {"description": "new"})

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert lifecycle_rig.logged_groups == 1  # committed to the log; the gateway applies it later


# --- Standard and Control edits and adds (S4) -----------------------------------------------

_STANDARD = "std_1"
_CONTROL = "ctl_1"


def _seed_tree(rig: LifecycleRig, *, legacy_edges: bool = False) -> None:
    rig.seed_policy(
        _POLICY,
        owner=_OWNER,
        standards=(
            SeedStandard(
                _STANDARD,
                "S",
                extra={"description": "std old", "procedure": "std proc"},
                controls=(
                    SeedControl(
                        _CONTROL, "C", extra={"description": "ctl old", "evidence_ref": "ctl ref"}
                    ),
                ),
            ),
        ),
        legacy_edges=legacy_edges,
    )


def _edit_standard(rig: LifecycleRig, fields: dict[str, object]) -> None:
    update_standard_draft(
        actor=_OWNER,
        standard_id=_STANDARD,
        fields=fields,
        graph=PolicyReadGraph(rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
    )


def _edit_control(rig: LifecycleRig, fields: dict[str, object]) -> None:
    update_control_draft(
        actor=_OWNER,
        control_id=_CONTROL,
        fields=fields,
        graph=PolicyReadGraph(rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
    )


def _add_standard(rig: LifecycleRig, title: str, fields: dict[str, object] | None = None) -> str:
    return add_standard_to_draft(
        actor=_OWNER,
        policy_id=_POLICY,
        title=title,
        fields=fields or {},
        graph=PolicyReadGraph(rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
    ).standard_id


def _add_control(
    rig: LifecycleRig,
    title: str,
    fields: dict[str, object] | None = None,
    control_type: Literal["automated", "manual"] = "manual",
) -> str:
    return add_control_to_draft(
        actor=_OWNER,
        standard_id=_STANDARD,
        title=title,
        control_type=control_type,
        fields=fields or {},
        graph=PolicyReadGraph(rig.graph),
        access_role_store=FakeAccessRoleStore(),
        submitter=rig.gateway,
    ).control_id


def test_a_cleared_standard_property_is_absent_afterwards_and_the_others_are_kept(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-007."""
    _seed_tree(lifecycle_rig)

    _edit_standard(lifecycle_rig, {"description": "new", "procedure": None})

    node = lifecycle_rig.graph.nodes[("Standard", _STANDARD)]
    assert "procedure" not in node
    assert node["description"] == "new"
    assert node["title"] == "S"


def test_a_cleared_control_property_is_absent_afterwards_and_the_others_are_kept(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-007."""
    _seed_tree(lifecycle_rig)

    _edit_control(lifecycle_rig, {"description": "new", "evidence_ref": None})

    node = lifecycle_rig.graph.nodes[("Control", _CONTROL)]
    assert "evidence_ref" not in node
    assert node["description"] == "new"
    assert node["title"] == "C"


def test_a_standard_or_control_patch_is_one_group_of_a_merge_then_a_remove_with_no_audit_link(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-008."""
    _seed_tree(lifecycle_rig)

    _edit_standard(lifecycle_rig, {"description": "new", "procedure": None})
    _edit_control(lifecycle_rig, {"description": "new", "evidence_ref": None})

    standard_group, control_group = lifecycle_rig.store.groups
    assert _ops(standard_group) == ["merge_property", "remove_property"]
    assert _ops(control_group) == ["merge_property", "remove_property"]
    assert lifecycle_rig.store.audit_link_of(standard_group) is None
    assert lifecycle_rig.store.audit_link_of(control_group) is None
    assert lifecycle_rig.audit.rows == []


def test_re_applying_a_standard_or_control_patch_logs_nothing(lifecycle_rig: LifecycleRig) -> None:
    _seed_tree(lifecycle_rig)
    _edit_standard(lifecycle_rig, {"description": "new", "procedure": None})
    _edit_control(lifecycle_rig, {"description": "new", "evidence_ref": None})
    logged = lifecycle_rig.logged_groups

    _edit_standard(lifecycle_rig, {"description": "new", "procedure": None})
    _edit_control(lifecycle_rig, {"description": "new", "evidence_ref": None})

    assert lifecycle_rig.logged_groups == logged


def test_the_first_standard_edit_of_a_graph_with_legacy_edges_stamps_them_first(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_tree(lifecycle_rig, legacy_edges=True)

    _edit_standard(lifecycle_rig, {"description": "new"})

    stamp, edit = lifecycle_rig.store.groups
    assert (_ops(stamp), _ops(edit)) == (["stamp_edge_identity"] * 2, ["merge_property"])
    assert lifecycle_rig.store.audit_link_of(stamp) is None


def test_a_standard_or_control_value_the_gateway_cannot_store_is_refused_with_the_persistence_error(
    lifecycle_rig: LifecycleRig,
) -> None:
    """Section 9 item 6."""
    _seed_tree(lifecycle_rig)

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as standard_raised:
        _edit_standard(lifecycle_rig, {"description": []})
    with pytest.raises(PolicyLifecycleGraphUnavailableError) as control_raised:
        _edit_control(lifecycle_rig, {"description": []})

    assert str(standard_raised.value).startswith("FalkorDB write failed: ")
    assert str(control_raised.value).startswith("FalkorDB write failed: ")
    assert lifecycle_rig.logged_groups == 0


def test_a_standard_edit_gateway_failure_is_the_persistence_error_with_the_cause_chained(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-011."""
    _seed_tree(lifecycle_rig)
    lifecycle_rig.store.fail_appends()

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _edit_standard(lifecycle_rig, {"description": "new"})

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert raised.value.__cause__ is not None
    assert lifecycle_rig.graph.nodes[("Standard", _STANDARD)]["description"] == "std old"


def test_a_control_edit_committed_but_not_applied_is_a_failure_of_the_call(
    lifecycle_rig: LifecycleRig,
) -> None:
    _seed_tree(lifecycle_rig)
    lifecycle_rig.graph.fail_on_write(redis.exceptions.ConnectionError("down"))

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _edit_control(lifecycle_rig, {"description": "new"})

    assert str(raised.value).startswith("FalkorDB write failed: ")


def test_adding_a_standard_is_one_group_of_a_node_then_its_parent_edge_with_no_audit_link(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-007, AC-BI-008."""
    _seed_tree(lifecycle_rig)

    new_id = _add_standard(lifecycle_rig, "Encryption Standard")

    (group,) = lifecycle_rig.store.groups
    assert _ops(group) == ["upsert_node", "upsert_edge"]
    assert lifecycle_rig.store.audit_link_of(group) is None
    assert lifecycle_rig.audit.rows == []
    assert new_id == standard_id(_POLICY, "Encryption Standard")
    assert (
        "SUPPORTED_BY",
        "Policy",
        _POLICY,
        "Standard",
        new_id,
        f"SUPPORTED_BY:{_POLICY}->{new_id}",
    ) in lifecycle_rig.graph.edges


def test_an_added_standard_gets_the_defaults_a_forced_status_and_drops_none_valued_keys(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-007."""
    _seed_tree(lifecycle_rig)

    plain = _add_standard(lifecycle_rig, "Plain")
    supplied = _add_standard(
        lifecycle_rig,
        "Supplied",
        {
            "implementation_status": "implemented",
            "status": "approved",
            "title": "x",
            "procedure": None,
        },
    )

    nodes = lifecycle_rig.graph.nodes
    assert nodes[("Standard", plain)] == {
        "implementation_status": "draft",
        "title": "Plain",
        "status": "draft",
    }
    assert nodes[("Standard", supplied)] == {
        "implementation_status": "implemented",
        "title": "Supplied",
        "status": "draft",
    }


def test_adding_a_control_is_one_group_with_planned_default_forced_type_and_status(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-007, AC-BI-008."""
    _seed_tree(lifecycle_rig)

    new_id = _add_control(
        lifecycle_rig,
        "Key Rotation",
        {"type": "manual", "status": "approved", "procedure": None},
        "automated",
    )

    (group,) = lifecycle_rig.store.groups
    assert _ops(group) == ["upsert_node", "upsert_edge"]
    assert lifecycle_rig.store.audit_link_of(group) is None
    assert new_id == control_id(_STANDARD, "Key Rotation")
    assert lifecycle_rig.graph.nodes[("Control", new_id)] == {
        "implementation_status": "planned",
        "title": "Key Rotation",
        "type": "automated",
        "status": "draft",
    }
    assert (
        "IMPLEMENTED_BY",
        "Standard",
        _STANDARD,
        "Control",
        new_id,
        f"IMPLEMENTED_BY:{_STANDARD}->{new_id}",
    ) in lifecycle_rig.graph.edges


def test_an_add_gateway_failure_is_the_persistence_error_and_writes_nothing(
    lifecycle_rig: LifecycleRig,
) -> None:
    """AC-BI-011."""
    _seed_tree(lifecycle_rig)
    lifecycle_rig.store.fail_appends()

    with pytest.raises(PolicyLifecycleGraphUnavailableError) as raised:
        _add_standard(lifecycle_rig, "New")

    assert str(raised.value).startswith("FalkorDB write failed: ")
    assert ("Standard", standard_id(_POLICY, "New")) not in lifecycle_rig.graph.nodes
