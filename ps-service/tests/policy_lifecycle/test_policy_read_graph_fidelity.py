"""the real `find_*`/`read_*` functions behave over `PolicyReadGraph` as expected.

The lifecycle tests that move onto the gateway rig read through `PolicyReadGraph`, an interpreter of
the eight fixed read statements. These tests run the REAL `graph_writer` read functions over seeded
trees and pin what the pre-existing fake-graph tests expect of them, so the interpreter's fidelity
is checked before any slice depends on it.
"""

from __future__ import annotations

import pytest

from policy_lifecycle._gateway_fakes import LifecycleRig, SeedControl, SeedStandard
from ps_service.policy_lifecycle import graph_writer


@pytest.fixture
def tree(lifecycle_rig: LifecycleRig) -> LifecycleRig:
    lifecycle_rig.seed_policy(
        "P",
        title="Access",
        status="approved",
        version="3",
        owner=("alice", "https://idp"),
        standards=(
            SeedStandard(
                "S1",
                "Standard one",
                status="approved",
                controls=(
                    SeedControl("C1", "Control one", status="approved", control_type="automated"),
                    SeedControl("C2", "Control two", status="approved", extra={"description": "d"}),
                ),
                extra={"description": "sd"},
            ),
            SeedStandard("S2", "Standard two", status="approved"),
        ),
    )
    return lifecycle_rig


def test_read_policy_tree_returns_the_policy_with_its_nested_standards_and_controls(
    tree: LifecycleRig,
) -> None:
    record = graph_writer.read_policy_tree(tree.read, "P")

    assert record == graph_writer.PolicyRecord(
        id="P",
        title="Access",
        status="approved",
        version="3",
        owner_subject="alice",
        owner_issuer="https://idp",
        standards=(
            graph_writer.StandardRecord(
                id="S1",
                title="Standard one",
                status="approved",
                controls=(
                    graph_writer.ControlRecord("C1", "Control one", "approved", "automated"),
                    graph_writer.ControlRecord("C2", "Control two", "approved", "manual"),
                ),
            ),
            graph_writer.StandardRecord(
                id="S2", title="Standard two", status="approved", controls=()
            ),
        ),
    )


def test_read_policy_tree_of_a_policy_with_no_standards_has_an_empty_tree(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy("P")

    record = graph_writer.read_policy_tree(lifecycle_rig.read, "P")

    assert record is not None
    assert record.standards == ()


def test_read_policy_tree_of_an_unknown_policy_is_none(lifecycle_rig: LifecycleRig) -> None:
    assert graph_writer.read_policy_tree(lifecycle_rig.read, "nope") is None


def test_find_existing_policy_returns_id_and_title_or_none(tree: LifecycleRig) -> None:
    assert graph_writer.find_existing_policy(tree.read, "P") == ("P", "Access")
    assert graph_writer.find_existing_policy(tree.read, "nope") is None


def test_find_approved_prior_returns_only_an_approved_predecessor(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy("old_draft", status="draft")
    lifecycle_rig.seed_policy("old_ok", status="approved")
    lifecycle_rig.seed_policy("new1")
    lifecycle_rig.seed_policy("new2")
    lifecycle_rig.seed_supersedes("old_draft", "new1")
    lifecycle_rig.seed_supersedes("old_ok", "new2")

    assert graph_writer.find_approved_prior(lifecycle_rig.read, "new1") is None
    assert graph_writer.find_approved_prior(lifecycle_rig.read, "new2") == "old_ok"


def test_read_fork_governance_lists_the_priors_governed_capabilities(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy("prior", status="approved")
    lifecycle_rig.seed_policy("fork")
    lifecycle_rig.seed_policy("bare_prior", status="approved")
    lifecycle_rig.seed_policy("bare_fork")
    lifecycle_rig.seed_supersedes("prior", "fork")
    lifecycle_rig.seed_supersedes("bare_prior", "bare_fork")
    for capability_id in ("cap_a", "cap_b"):
        lifecycle_rig.seed_capability(capability_id)
        lifecycle_rig.seed_governs(capability_id, "prior")

    assert graph_writer.read_fork_governance(lifecycle_rig.read, "fork") == (
        graph_writer.ForkGovernance("prior", ("cap_a", "cap_b"))
    )
    assert graph_writer.read_fork_governance(lifecycle_rig.read, "bare_fork") == (
        graph_writer.ForkGovernance("bare_prior", ())
    )
    assert graph_writer.read_fork_governance(lifecycle_rig.read, "prior") is None


def test_read_capability_governors_omits_unknown_and_absorbed_and_maps_ungoverned_to_none(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy("P")
    lifecycle_rig.seed_capability("governed")
    lifecycle_rig.seed_capability("free")
    lifecycle_rig.graph.seed_node(("MergedCapability",), "absorbed", {"merged_into": "governed"})
    lifecycle_rig.seed_governs("governed", "P")

    governors = graph_writer.read_capability_governors(
        lifecycle_rig.read, ("governed", "free", "absorbed", "missing")
    )

    assert governors == {"governed": "P", "free": None}


def test_read_policy_tree_for_fork_returns_every_nodes_full_properties(tree: LifecycleRig) -> None:
    standards = graph_writer.read_policy_tree_for_fork(tree.read, "P")

    assert [standard.title for standard in standards] == ["Standard one", "Standard two"]
    first = standards[0]
    assert first.properties == {
        "id": "S1",
        "title": "Standard one",
        "status": "approved",
        "description": "sd",
    }
    assert [control.title for control in first.controls] == ["Control one", "Control two"]
    assert first.controls[1].properties["description"] == "d"
    assert standards[1].controls == ()


def test_read_policy_tree_for_fork_of_a_policy_with_no_standards_is_empty(
    lifecycle_rig: LifecycleRig,
) -> None:
    lifecycle_rig.seed_policy("P")

    assert graph_writer.read_policy_tree_for_fork(lifecycle_rig.read, "P") == ()


def test_find_standard_with_parent_returns_the_parents_owner_and_status(tree: LifecycleRig) -> None:
    found = graph_writer.find_standard_with_parent(tree.read, "S1")

    assert found == graph_writer.StandardWithParent(
        policy_id="P",
        policy_owner_subject="alice",
        policy_owner_issuer="https://idp",
        policy_status="approved",
        standard_status="approved",
        standard_title="Standard one",
    )
    assert graph_writer.find_standard_with_parent(tree.read, "nope") is None


def test_find_control_with_parent_walks_two_hops_to_the_root_policy(tree: LifecycleRig) -> None:
    found = graph_writer.find_control_with_parent(tree.read, "C2")

    assert found == graph_writer.ControlWithParent(
        standard_id="S1",
        policy_id="P",
        policy_owner_subject="alice",
        policy_owner_issuer="https://idp",
        policy_status="approved",
        control_status="approved",
        control_title="Control two",
    )
    assert graph_writer.find_control_with_parent(tree.read, "nope") is None
