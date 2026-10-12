# pyright: reportPrivateUsage=false
"""`CleanupReadGraph` answers the reads exactly as the statements say (issue #212).

The double is only worth having if it is faithful: each read it was taught is pinned here against
the rows the statement's `RETURN` clause names, and anything else (a write, or a read it was not
taught) fails loudly instead of returning nothing.
"""

from __future__ import annotations

from typing import cast

import pytest

from graph_cleanup._fakes import ABSORBED, OBL_ABSORBED, OBL_SURVIVOR, ROLE_ID, SURVIVOR
from graph_cleanup._gateway_fakes import OTHER_CAPABILITY, POLICY, CleanupRig
from graph_cleanup._unmerge_runs import (
    capability_unmerge_world,
    obligation_unmerge_world,
    unmerge_plan,
)
from ps_service.graph_cleanup import graph_reader
from ps_service.graph_cleanup.graph_reader import (
    read_capability_merge_state,
    read_capability_tombstone,
    read_capability_unmerge_state,
    read_capability_unmerged,
    read_governed_by_edge_present,
    read_obligation_marker,
    read_obligation_marker_present,
    read_obligation_merge_state,
    read_obligation_present,
    read_obligation_unmerge_state,
    read_release_state,
)
from ps_service.graph_cleanup.models import CapabilityUnmergePlan, ObligationUnmergePlan


def test_the_capability_node_read_returns_id_name_status_and_the_scalar_properties(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()

    rows = cleanup_rig.read.query(
        graph_reader._CAPABILITY_NODES_QUERY, {"ids": [SURVIVOR, "cap_missing"]}
    ).result_set

    assert rows == [[SURVIVOR, "Survivor", "active", "desc", None, 0.9]]


def test_a_node_without_a_status_reads_as_active(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_capability(ABSORBED)

    [row] = cleanup_rig.read.query(
        graph_reader._CAPABILITY_NODES_QUERY, {"ids": [ABSORBED]}
    ).result_set

    assert cast("list[object]", row)[2] == "active"


def test_the_governors_read_returns_one_row_per_governed_by_edge(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_release_world()

    rows = cleanup_rig.read.query(
        graph_reader._CAPABILITY_GOVERNORS_QUERY, {"ids": [SURVIVOR]}
    ).result_set

    assert rows == [[SURVIVOR, POLICY, "Incident Policy", "draft"]]


def test_the_governed_set_read_returns_the_policy_and_each_capability_it_governs(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()

    rows = cleanup_rig.read.query(
        graph_reader._POLICY_GOVERNED_SETS_QUERY, {"policy_ids": [POLICY]}
    ).result_set

    assert rows == [[POLICY, SURVIVOR], [POLICY, OTHER_CAPABILITY]]


def test_the_edge_count_counts_the_governed_by_edges_between_the_two(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()

    assert read_governed_by_edge_present(cleanup_rig.read, capability_id=SURVIVOR, policy_id=POLICY)
    assert not read_governed_by_edge_present(
        cleanup_rig.read, capability_id=ABSORBED, policy_id=POLICY
    )


def test_the_real_release_read_over_the_double_finds_the_capability_and_its_governed_set(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_release_world()

    state = read_release_state(cleanup_rig.read, capability_id=SURVIVOR)

    assert state.capability is not None
    assert state.capability.properties == {"description": "desc", "confidence": 0.9}
    assert [p.id for p in state.policies] == [POLICY]
    assert state.governed_set == (OTHER_CAPABILITY, SURVIVOR)


def test_each_incoming_edge_read_returns_source_and_target_of_the_edges_into_the_capabilities(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_capability_merge_world()
    params: dict[str, object] = {"ids": sorted({SURVIVOR, ABSORBED})}

    rows = {
        rel_type: cleanup_rig.read.query(query, params).result_set
        for rel_type, _label, query in graph_reader._INCOMING_EDGE_QUERIES
    }

    assert rows == {
        "REQUIRES": [["obl_1", ABSORBED], ["obl_2", ABSORBED], ["obl_2", SURVIVOR]],
        "COVERS": [["pa_1", ABSORBED]],
        "MITIGATED_BY": [],
    }


def test_the_real_capability_merge_read_over_the_double_sees_edges_and_governors(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_capability_merge_world(governed=((ABSORBED, POLICY, "approved"),))

    state = read_capability_merge_state(
        cleanup_rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED
    )

    assert state.survivor is not None
    assert state.survivor.properties == {"description": "desc", "confidence": 0.9}
    assert state.absorbed is not None
    assert state.absorbed.properties == {"type": "technical", "confidence": 0.8}
    assert len(state.edges) == 4
    assert [p.id for p in state.absorbed_policies] == [POLICY]
    assert state.survivor_policies == ()
    assert state.governed_sets == {POLICY: (ABSORBED, OTHER_CAPABILITY)}


def test_the_tombstone_count_needs_a_merged_node_and_the_merged_into_edge(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_capability_merge_world()

    assert not read_capability_tombstone(
        cleanup_rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED
    )
    cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] = "merged"
    assert not read_capability_tombstone(
        cleanup_rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED
    )
    cleanup_rig.seed_edge(
        ("MERGED_INTO", "Capability", ABSORBED, "Capability", SURVIVOR), legacy=False
    )
    assert read_capability_tombstone(cleanup_rig.read, survivor_id=SURVIVOR, absorbed_id=ABSORBED)


def test_the_real_obligation_merge_read_over_the_double_sees_roles_edges_and_source_refs(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_obligation_merge_world()

    state = read_obligation_merge_state(
        cleanup_rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED
    )

    assert state.survivor is not None
    assert (state.survivor.text, state.survivor.properties) == (
        "Report incidents",
        {"confidence": 0.9},
    )
    assert state.absorbed is not None
    assert state.absorbed.text == "Report  incidents."
    assert [(r.id, r.name) for r in state.absorbed_roles] == [(ROLE_ID, "Manufacturer")]
    assert {(e.rel_type, e.source_id, e.target_id) for e in state.edges} == {
        ("SATISFIED_BY", "req_1", OBL_ABSORBED),
        ("SATISFIED_BY", "req_2", OBL_ABSORBED),
        ("SATISFIED_BY", "req_2", OBL_SURVIVOR),
        ("REQUIRES", OBL_ABSORBED, "cap_1"),
        ("REQUIRES", OBL_SURVIVOR, "cap_1"),
    }
    assert [(r.requirement_id, r.source_ref) for r in state.absorbed_requirement_refs] == [
        ("req_1", "Art. 6(1)"),
        ("req_2", "Art. 6(2)"),
    ]


def test_a_requirement_without_an_expressing_instrument_reads_a_null_source_ref(
    cleanup_rig: CleanupRig,
) -> None:
    cleanup_rig.seed_obligation_merge_world()
    cleanup_rig.seed_endpoint("Requirement", "req_orphan")
    cleanup_rig.seed_edge(
        ("SATISFIED_BY", "Requirement", "req_orphan", "Obligation", OBL_ABSORBED), legacy=False
    )

    rows = cleanup_rig.read.query(
        graph_reader._OBLIGATION_REFS_QUERY, {"obligation_id": OBL_ABSORBED}
    ).result_set

    assert ["req_orphan", None] in rows


def test_the_marker_and_presence_counts_follow_the_graph(cleanup_rig: CleanupRig) -> None:
    cleanup_rig.seed_obligation_merge_world()

    assert read_obligation_present(cleanup_rig.read, obligation_id=OBL_ABSORBED)
    assert not read_obligation_marker_present(cleanup_rig.read, obligation_id=OBL_ABSORBED)
    cleanup_rig.seed_endpoint("MergedObligation", OBL_ABSORBED, merged_into=OBL_SURVIVOR)
    assert read_obligation_marker_present(cleanup_rig.read, obligation_id=OBL_ABSORBED)
    assert read_obligation_marker(
        cleanup_rig.read, survivor_id=OBL_SURVIVOR, absorbed_id=OBL_ABSORBED
    )
    assert not read_obligation_marker(
        cleanup_rig.read, survivor_id="obl_else", absorbed_id=OBL_ABSORBED
    )


def test_the_real_capability_unmerge_read_over_the_double_sees_redirects_endpoints_and_policy(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig, policy="moved")
    plan = unmerge_plan(cleanup_rig, ABSORBED)
    assert isinstance(plan, CapabilityUnmergePlan)

    state = read_capability_unmerge_state(cleanup_rig.read, inputs=plan.inputs)

    assert state.absorbed is not None
    assert state.absorbed.status == "merged"
    assert state.survivor is not None
    assert state.survivor.status == "active"
    assert state.redirects == {ABSORBED: (SURVIVOR,)}
    assert {(e.rel_type, e.source_id) for e in state.survivor_edges} == {
        ("REQUIRES", "obl_1"),
        ("REQUIRES", "obl_2"),
        ("COVERS", "pa_1"),
        ("MITIGATED_BY", "rp_1"),
    }
    assert state.survivor_policy_ids == (POLICY,)
    assert state.existing_endpoints == {
        "REQUIRES": ("obl_1", "obl_2"),
        "COVERS": ("pa_1",),
        "MITIGATED_BY": ("rp_1",),
    }
    assert state.policy_exists is True


def test_an_endpoint_read_returns_only_nodes_of_its_own_label(cleanup_rig: CleanupRig) -> None:
    capability_unmerge_world(cleanup_rig)

    rows = {
        rel_type: cleanup_rig.read.query(query, {"ids": ["obl_1", "pa_1", "rp_1"]}).result_set
        for rel_type, query in graph_reader._ENDPOINT_QUERIES
    }

    assert rows == {"REQUIRES": [["obl_1"]], "COVERS": [["pa_1"]], "MITIGATED_BY": [["rp_1"]]}


def test_the_unmerged_read_counts_an_active_node_and_its_merged_into_edges(
    cleanup_rig: CleanupRig,
) -> None:
    capability_unmerge_world(cleanup_rig)

    assert not read_capability_unmerged(cleanup_rig.read, capability_id=ABSORBED)  # a tombstone
    cleanup_rig.graph.nodes[("Capability", ABSORBED)]["status"] = "active"
    assert not read_capability_unmerged(
        cleanup_rig.read, capability_id=ABSORBED
    )  # still redirected
    del cleanup_rig.graph.edges[next(k for k in cleanup_rig.graph.edges if k[0] == "MERGED_INTO")]
    assert read_capability_unmerged(cleanup_rig.read, capability_id=ABSORBED)


def test_the_real_obligation_unmerge_read_over_the_double_sees_marker_role_and_endpoints(
    cleanup_rig: CleanupRig,
) -> None:
    obligation_unmerge_world(cleanup_rig)
    plan = unmerge_plan(cleanup_rig, OBL_ABSORBED)
    assert isinstance(plan, ObligationUnmergePlan)

    state = read_obligation_unmerge_state(cleanup_rig.read, inputs=plan.inputs)

    assert state.absorbed_exists is False
    assert state.survivor_exists is True
    assert state.survivor_text == "Report incidents"
    assert state.marker_targets == {OBL_ABSORBED: (OBL_SURVIVOR,)}
    assert state.role_exists is True
    assert state.existing_requirements == ("req_1", "req_2")
    assert state.capability_statuses == {"cap_1": "active"}
    assert {(e.rel_type, e.source_id, e.target_id) for e in state.survivor_edges} == {
        ("SATISFIED_BY", "req_1", OBL_SURVIVOR),
        ("SATISFIED_BY", "req_2", OBL_SURVIVOR),
        ("REQUIRES", OBL_SURVIVOR, "cap_1"),
    }


def test_a_write_statement_is_refused(cleanup_rig: CleanupRig) -> None:
    with pytest.raises(AssertionError, match="refuses a write statement"):
        cleanup_rig.read.query("MATCH (c:Capability) DELETE c", {})


def test_a_read_the_double_was_not_taught_is_refused(cleanup_rig: CleanupRig) -> None:
    with pytest.raises(AssertionError, match="not a cleanup read statement"):
        cleanup_rig.read.query("MATCH (c:Capability) RETURN c.id", {})
