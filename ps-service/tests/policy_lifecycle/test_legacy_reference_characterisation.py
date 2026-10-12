"""The frozen legacy writer is the original one, and its interpreter applies Cypher like FalkorDB.

`_legacy_reference` is the pre-#211 `graph_writer.py` write path, frozen, run against an
interpreter (issue #211, S0). This file proves:

* the frozen statements are the original ones (sha256 pins taken from `git show HEAD:`);
* the interpreter applies each clause the way FalkorDB does, with the behaviours characterised on a
  live FalkorDB on 2026-10-08 (a re-added child under the same parent is one node, a cascade over a
  Policy with no Standards still sets the Policy, a failed repoint/claim guard returns no row and
  writes nothing, `SET x.k = null` removes the key), and rejects any shape it was not taught.

Met modulo edge identity; the live digest and live interpreter agreement are not verifiable here
(no FalkorDB in this sandbox).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from policy_lifecycle._gateway_fakes import LifecycleRig, SeedControl, SeedStandard
from policy_lifecycle._legacy_reference import (
    FROZEN_STATEMENT_HASHES,
    FROZEN_STATEMENTS,
    LegacyRecordingGraph,
    legacy_add_control_to_standard,
    legacy_add_standard_to_policy,
    legacy_approve_fork_repoint,
    legacy_backfill_governance_status,
    legacy_cascade_status,
    legacy_create_policy_draft,
    legacy_update_control_fields,
    legacy_update_policy_fields,
    legacy_update_standard_fields,
)
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.policy_lifecycle.graph_writer import ControlDraft, StandardDraft

if TYPE_CHECKING:
    from collections.abc import Callable

_OWNER = ("sub", "iss")


@dataclass
class _Result:
    result_set: list[object] = field(default_factory=lambda: [[1]])


@dataclass
class _Capturing:
    statements: list[str] = field(default_factory=list)

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        del params
        self.statements.append(q)
        return _Result()


def _tree(*, status: str | None = "draft") -> LegacyRecordingGraph:
    """P with S1 (C1, C2) and S2 (no controls), seeded as a pre-gateway writer left it."""
    rig = LifecycleRig()
    rig.seed_policy(
        "P",
        status=status,
        standards=(
            SeedStandard(
                "S1",
                "one",
                status=status,
                controls=(
                    SeedControl("C1", "c1", status=status),
                    SeedControl("C2", "c2", status=status),
                ),
            ),
            SeedStandard("S2", "two", status=status),
        ),
    )
    return LegacyRecordingGraph.copy_of(rig.graph)


def _statuses(graph: LegacyRecordingGraph) -> dict[str, object]:
    return {f"{label}:{key}": props.get("status") for (label, key), props in graph.nodes.items()}


def test_frozen_statements_match_their_pinned_hashes() -> None:
    capture = _Capturing()
    standard = StandardDraft(id="s", title="t", controls=(ControlDraft("c", "t", "manual"),))

    legacy_backfill_governance_status(capture, "p")
    legacy_cascade_status(capture, policy_id="p", target_status="x")
    legacy_approve_fork_repoint(
        capture, policy_id="p", prior_id="q", capability_ids=("a",), target_status="x"
    )
    legacy_create_policy_draft(
        capture, policy_id="p", title="t", owner=_OWNER, capability_ids=("c",)
    )
    legacy_create_policy_draft(
        capture,
        policy_id="p",
        title="t",
        owner=_OWNER,
        standards=(standard,),
        supersedes_policy_id="old",
    )
    legacy_update_policy_fields(capture, policy_id="p", properties={"a": 1, "k": None})
    legacy_update_standard_fields(capture, standard_id="s", properties={"a": 1, "k": None})
    legacy_update_control_fields(capture, control_id="c", properties={"a": 1, "k": None})

    hashes = [hashlib.sha256(statement.encode()).hexdigest() for statement in capture.statements]
    assert hashes == [
        FROZEN_STATEMENT_HASHES[name]
        for name in (
            "backfill_version",
            "backfill_standard_status",
            "backfill_control_status",
            "cascade",
            "repoint",
            "claim_create",
            "plain_create",
            "supersede",
            "standard_child",
            "control_child",
            "update_policy_set",
            "clear_policy_key",
            "update_standard_set",
            "clear_standard_key",
            "update_control_set",
            "clear_control_key",
        )
    ]


def test_the_statement_table_agrees_with_its_pins() -> None:
    for name, text in FROZEN_STATEMENTS.items():
        assert hashlib.sha256(text.encode()).hexdigest() == FROZEN_STATEMENT_HASHES[name], name


def test_the_interpreter_rejects_a_statement_shape_it_was_not_taught() -> None:
    graph = LegacyRecordingGraph()

    with pytest.raises(AssertionError, match="not a frozen legacy statement"):
        graph.query("MATCH (n:Policy {id: $id}) DETACH DELETE n", {"id": "P"})


def test_adding_the_same_standard_twice_under_one_parent_leaves_one_node_and_one_edge() -> None:
    graph = _tree()

    for title in ("again", "again"):
        legacy_add_standard_to_policy(
            graph, policy_id="P", standard_id="S1", title=title, extra_properties={}
        )

    standards = [key for key in graph.nodes if key[0] == "Standard"]
    edges = [key for key in graph.edges if key[0] == "SUPPORTED_BY" and key[4] == "S1"]
    assert (len(standards), len(edges)) == (2, 1)
    assert graph.nodes[("Standard", "S1")]["title"] == "again"


def test_a_standard_id_forced_under_another_parent_is_the_unmodelled_latent_behaviour() -> None:
    graph = _tree()
    graph.nodes[("Policy", "Q")] = {"title": "q"}

    with pytest.raises(AssertionError, match="second Standard node"):
        legacy_add_standard_to_policy(
            graph, policy_id="Q", standard_id="S1", title="x", extra_properties={}
        )


def test_a_cascade_over_a_policy_with_no_standards_still_sets_the_policy() -> None:
    graph = LegacyRecordingGraph()
    graph.nodes[("Policy", "P")] = {"status": "draft"}

    legacy_cascade_status(graph, policy_id="P", target_status="proposed")

    assert graph.nodes[("Policy", "P")]["status"] == "proposed"


def test_a_cascade_sets_the_policy_every_standard_and_every_control() -> None:
    graph = _tree()

    legacy_cascade_status(graph, policy_id="P", target_status="proposed")

    assert set(_statuses(graph).values()) == {"proposed"}
    assert len(graph.nodes) == 5


def test_a_cascade_of_an_unknown_policy_writes_nothing() -> None:
    graph = _tree()
    before = canonical_digest(graph.as_gateway_graph())

    legacy_cascade_status(graph, policy_id="nope", target_status="proposed")

    assert canonical_digest(graph.as_gateway_graph()) == before


def _fork_graph(*, governed: tuple[str, ...] = ("cap_a", "cap_b")) -> LegacyRecordingGraph:
    rig = LifecycleRig()
    rig.seed_policy("prior", status="approved", standards=(SeedStandard("S0", "old"),))
    rig.seed_policy("fork", status="proposed", standards=(SeedStandard("S9", "new"),))
    rig.seed_supersedes("prior", "fork")
    for capability_id in ("cap_a", "cap_b", "cap_c"):
        rig.seed_capability(capability_id)
    for capability_id in governed:
        rig.seed_governs(capability_id, "prior")
    return LegacyRecordingGraph.copy_of(rig.graph)


def test_a_repoint_moves_the_governed_edges_and_cascades() -> None:
    graph = _fork_graph()

    moved = legacy_approve_fork_repoint(
        graph,
        policy_id="fork",
        prior_id="prior",
        capability_ids=("cap_a", "cap_b"),
        target_status="approved",
    )

    governors = {key[2]: key[4] for key in graph.edges if key[0] == "GOVERNED_BY"}
    assert (moved, governors) == (True, {"cap_a": "fork", "cap_b": "fork"})
    assert (
        graph.nodes[("Policy", "fork")]["status"],
        graph.nodes[("Standard", "S9")]["status"],
    ) == (
        "approved",
        "approved",
    )


def test_a_repoint_whose_governed_set_changed_returns_no_row_and_writes_nothing() -> None:
    graph = _fork_graph(governed=("cap_a",))  # cap_b is not governed by the prior any more
    before = canonical_digest(graph.as_gateway_graph())

    moved = legacy_approve_fork_repoint(
        graph,
        policy_id="fork",
        prior_id="prior",
        capability_ids=("cap_a", "cap_b"),
        target_status="approved",
    )

    assert (moved, canonical_digest(graph.as_gateway_graph())) == (False, before)


def test_a_parallel_governed_by_edge_makes_the_repoint_count_mismatch() -> None:
    graph = _fork_graph()
    graph.seed_parallel_edge(("GOVERNED_BY", "Capability", "cap_a", "Policy", "prior"))

    moved = legacy_approve_fork_repoint(
        graph,
        policy_id="fork",
        prior_id="prior",
        capability_ids=("cap_a", "cap_b"),
        target_status="approved",
    )

    assert moved is False


def test_a_repoint_with_no_governed_capabilities_still_cascades() -> None:
    graph = _fork_graph(governed=())

    moved = legacy_approve_fork_repoint(
        graph, policy_id="fork", prior_id="prior", capability_ids=(), target_status="approved"
    )

    assert (moved, graph.nodes[("Policy", "fork")]["status"]) == (True, "approved")


def _claim(graph: LegacyRecordingGraph, *ids: str) -> bool:
    return legacy_create_policy_draft(
        graph, policy_id="NEW", title="New", owner=_OWNER, capability_ids=ids
    )


def test_a_claim_create_over_ungoverned_capabilities_writes_the_policy_and_the_edges() -> None:
    graph = _fork_graph(governed=())

    created = _claim(graph, "cap_a", "cap_b")

    assert created is True
    assert graph.nodes[("Policy", "NEW")]["status"] == "draft"
    assert {key[2] for key in graph.edges if key[0] == "GOVERNED_BY" and key[4] == "NEW"} == {
        "cap_a",
        "cap_b",
    }


def test_a_claim_create_over_a_governed_capability_returns_false_and_writes_nothing() -> None:
    graph = _fork_graph(governed=("cap_b",))
    before = canonical_digest(graph.as_gateway_graph())

    created = _claim(graph, "cap_a", "cap_b")

    assert (created, canonical_digest(graph.as_gateway_graph())) == (False, before)


def test_a_claim_create_over_a_merged_tombstone_returns_false_and_writes_nothing() -> None:
    graph = _fork_graph(governed=())
    graph.nodes[("Capability", "cap_b")]["status"] = "merged"
    before = canonical_digest(graph.as_gateway_graph())

    created = _claim(graph, "cap_a", "cap_b")

    assert (created, canonical_digest(graph.as_gateway_graph())) == (False, before)


def test_a_claim_create_over_an_unknown_capability_returns_false() -> None:
    graph = _fork_graph(governed=())

    assert _claim(graph, "cap_a", "cap_missing") is False


def test_a_plain_create_writes_the_tree_with_children_forced_to_draft() -> None:
    graph = LegacyRecordingGraph()
    standard = StandardDraft(
        id="S",
        title="std",
        controls=(
            ControlDraft(
                "C", "ctl", "manual", extra_properties={"status": "approved", "note": "kept"}
            ),
        ),
        extra_properties={"title": "ignored", "description": "d"},
    )

    created = legacy_create_policy_draft(
        graph, policy_id="NEW", title="New", owner=_OWNER, standards=(standard,)
    )

    assert created is True
    assert graph.nodes[("Policy", "NEW")] == {
        "title": "New",
        "status": "draft",
        "version": "1",
        "owner_subject": "sub",
        "owner_issuer": "iss",
    }
    assert graph.nodes[("Standard", "S")] == {"title": "std", "status": "draft", "description": "d"}
    assert graph.nodes[("Control", "C")] == {
        "title": "ctl",
        "type": "manual",
        "status": "draft",
        "note": "kept",
    }
    assert set(graph.edges) == {
        ("SUPPORTED_BY", "Policy", "NEW", "Standard", "S"),
        ("IMPLEMENTED_BY", "Standard", "S", "Control", "C"),
    }


def test_a_fork_create_links_the_prior_with_a_supersedes_edge() -> None:
    graph = LegacyRecordingGraph()
    graph.nodes[("Policy", "OLD")] = {"status": "approved"}

    legacy_create_policy_draft(
        graph, policy_id="NEW", title="New", owner=_OWNER, supersedes_policy_id="OLD", version="2"
    )

    assert ("SUPERSEDED_BY", "Policy", "OLD", "Policy", "NEW") in graph.edges
    assert graph.nodes[("Policy", "NEW")]["version"] == "2"


@pytest.mark.parametrize(
    ("label", "update", "id_name"),
    [
        ("Policy", legacy_update_policy_fields, "policy_id"),
        ("Standard", legacy_update_standard_fields, "standard_id"),
        ("Control", legacy_update_control_fields, "control_id"),
    ],
)
def test_a_patch_sets_the_given_keys_and_a_null_key_removes_the_property(
    label: str,
    update: Callable[..., None],
    id_name: str,
) -> None:
    graph = LegacyRecordingGraph()
    graph.nodes[(label, "X")] = {"description": "old", "owner_note": "old", "kept": "yes"}

    update(graph, **{id_name: "X"}, properties={"description": "new", "owner_note": None})

    assert graph.nodes[(label, "X")] == {"description": "new", "kept": "yes"}


def test_a_patch_of_an_unknown_node_writes_nothing() -> None:
    graph = LegacyRecordingGraph()

    legacy_update_policy_fields(graph, policy_id="nope", properties={"description": "x", "k": None})

    assert graph.nodes == {}


def test_add_control_defaults_implementation_status_to_planned_and_forces_the_rest() -> None:
    graph = _tree()

    legacy_add_control_to_standard(
        graph,
        standard_id="S2",
        control_id="C9",
        title="nine",
        control_type="automated",
        extra_properties={"description": "d", "execution_frequency": None, "status": "approved"},
    )

    assert graph.nodes[("Control", "C9")] == {
        "description": "d",
        "implementation_status": "planned",
        "title": "nine",
        "type": "automated",
        "status": "draft",
    }
    assert ("IMPLEMENTED_BY", "Standard", "S2", "Control", "C9") in graph.edges


def test_add_standard_defaults_implementation_status_to_draft() -> None:
    graph = _tree()

    legacy_add_standard_to_policy(
        graph, policy_id="P", standard_id="S9", title="nine", extra_properties={}
    )

    assert graph.nodes[("Standard", "S9")] == {
        "implementation_status": "draft",
        "title": "nine",
        "status": "draft",
    }


def test_the_backfill_fills_a_missing_version_and_statuses_from_the_root_policy() -> None:
    graph = _tree(status=None)
    graph.nodes[("Policy", "P")]["status"] = "approved"
    graph.nodes[("Standard", "S1")]["status"] = "draft"  # already set: never overwritten

    legacy_backfill_governance_status(graph, "P")

    assert graph.nodes[("Policy", "P")]["version"] == "1"
    assert _statuses(graph) == {
        "Policy:P": "approved",
        "Standard:S1": "draft",
        "Standard:S2": "approved",
        "Control:C1": "approved",
        "Control:C2": "approved",
    }


def test_the_backfill_keeps_an_existing_version_and_is_idempotent() -> None:
    graph = _tree()
    graph.nodes[("Policy", "P")]["version"] = "7"
    legacy_backfill_governance_status(graph, "P")
    once = canonical_digest(graph.as_gateway_graph())

    legacy_backfill_governance_status(graph, "P")

    assert graph.nodes[("Policy", "P")]["version"] == "7"
    assert canonical_digest(graph.as_gateway_graph()) == once


def test_the_backfill_of_a_policy_with_no_standards_sets_only_the_version() -> None:
    graph = LegacyRecordingGraph()
    graph.nodes[("Policy", "P")] = {"status": "draft"}

    legacy_backfill_governance_status(graph, "P")

    assert graph.nodes[("Policy", "P")] == {"status": "draft", "version": "1"}
