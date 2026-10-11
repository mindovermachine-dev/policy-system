"""Tests for the embedding backfill writers of `ps_service.company_merge.graph_writer`.

PLAN_REVIEWED.md §10 Increment 11 (backfill half, B2's fix) and issue #31 (the incoming-side
twin). Since issue #210 a backfill is a Graph Write Gateway group: the writer reads which of the
named nodes exist and still have no embedding (one batched read per call, never the vector), and
submits `UpsertNode(label, id, embedding=<vector>)` only for those. A node that already has an
embedding, or does not exist, gets nothing (AC-BI-006): the gateway would merge over an existing
embedding and would create an absent node, so set-if-null cannot be left to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from company_merge._gateway_fakes import BASELINE, POLICY_SYSTEM
from company_merge._seed_merge_graphs import SingleTenantStore
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.graph_writer import (
    backfill_canonical_embeddings,
    backfill_incoming_capability_embeddings,
    build_embedding_backfill_primitives,
)
from ps_service.graph_gateway.digest import canonical_digest

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig


def _target(rig: MergeRig, graph_name: str = POLICY_SYSTEM) -> GraphWriteTarget:
    return GraphWriteTarget(rig.gateway, graph_name, rig.audit_event_id)


def _node(rig: MergeRig, node_id: str, embedding: list[float] | None = None) -> None:
    properties: dict[str, object] = {"name": node_id}
    if embedding is not None:
        properties["embedding"] = embedding
    rig.policy_system.seed_node(("Capability",), node_id, properties)


def _logged_ids(rig: MergeRig) -> list[str]:
    return [entry.identity for entry in rig.logged()]


def test_a_node_without_an_embedding_receives_it_and_no_other_property_changes(
    merge_rig: MergeRig,
) -> None:
    _node(merge_rig, "capability_1")

    backfill_canonical_embeddings(
        _target(merge_rig),
        merge_rig.legacy,
        kind="Capability",
        embeddings={"capability_1": (0.1, 0.2)},
    )

    [entry] = merge_rig.logged()
    assert (entry.name, entry.identity, entry.embedding) == (
        "Capability",
        "capability_1",
        (0.1, 0.2),
    )
    assert entry.content["properties"] == {}
    assert merge_rig.policy_system.nodes[("Capability", "capability_1")] == {
        "name": "capability_1",
        "embedding": [0.1, 0.2],
    }


def test_a_node_that_already_has_an_embedding_is_unchanged_and_nothing_is_logged(
    merge_rig: MergeRig,
) -> None:
    _node(merge_rig, "capability_1", embedding=[0.9, 0.9])
    before = canonical_digest(merge_rig.policy_system)

    backfill_canonical_embeddings(
        _target(merge_rig),
        merge_rig.legacy,
        kind="Capability",
        embeddings={"capability_1": (0.1, 0.2)},
    )

    assert merge_rig.logged() == []
    assert merge_rig.groups == []
    assert canonical_digest(merge_rig.policy_system) == before
    assert merge_rig.policy_system.nodes[("Capability", "capability_1")]["embedding"] == [0.9, 0.9]


def test_a_backfill_for_an_absent_node_creates_nothing(merge_rig: MergeRig) -> None:
    backfill_canonical_embeddings(
        _target(merge_rig),
        merge_rig.legacy,
        kind="Capability",
        embeddings={"capability_gone": (0.1,)},
    )

    assert merge_rig.logged() == []
    assert ("Capability", "capability_gone") not in merge_rig.policy_system.nodes


def test_one_group_carries_one_primitive_per_eligible_id_in_input_order(
    merge_rig: MergeRig,
) -> None:
    _node(merge_rig, "capability_1")
    _node(merge_rig, "capability_2", embedding=[0.5])
    _node(merge_rig, "capability_3")

    backfill_canonical_embeddings(
        _target(merge_rig),
        merge_rig.legacy,
        kind="Capability",
        embeddings={
            "capability_3": (0.3, 0.4),
            "capability_2": (0.7,),
            "capability_missing": (0.8,),
            "capability_1": (0.1, 0.2),
        },
    )

    assert _logged_ids(merge_rig) == ["capability_3", "capability_1"]
    assert len(merge_rig.groups) == 1


def test_the_state_read_is_one_batched_read_that_never_returns_the_vector(
    merge_rig: MergeRig,
) -> None:
    _node(merge_rig, "capability_1")
    _node(merge_rig, "capability_2")

    backfill_canonical_embeddings(
        _target(merge_rig),
        merge_rig.legacy,
        kind="Capability",
        embeddings={"capability_1": (0.1,), "capability_2": (0.2,)},
    )

    reads = [q for q in merge_rig.legacy.statements if "embedding IS NULL" in q]
    assert reads == ["MATCH (n:Capability) WHERE n.id IN $ids RETURN n.id, n.embedding IS NULL"]
    assert merge_rig.legacy.params_seen[-1] == {"ids": ["capability_1", "capability_2"]}


def test_backfill_with_empty_embeddings_reads_and_writes_nothing(merge_rig: MergeRig) -> None:
    backfill_canonical_embeddings(
        _target(merge_rig), merge_rig.legacy, kind="Capability", embeddings={}
    )

    assert merge_rig.legacy.statements == []
    assert merge_rig.groups == []


def test_a_second_backfill_with_the_same_input_logs_nothing_more(merge_rig: MergeRig) -> None:
    _node(merge_rig, "capability_1")
    embeddings = {"capability_1": (0.1, 0.2)}

    backfill_canonical_embeddings(
        _target(merge_rig), merge_rig.legacy, kind="Capability", embeddings=embeddings
    )
    backfill_canonical_embeddings(
        _target(merge_rig), merge_rig.legacy, kind="Capability", embeddings=embeddings
    )

    assert _logged_ids(merge_rig) == ["capability_1"]


def test_policy_kind_backfills_a_policy_node(merge_rig: MergeRig) -> None:
    merge_rig.policy_system.seed_node(("Policy",), "policy_1", {"title": "p"})

    backfill_canonical_embeddings(
        _target(merge_rig), merge_rig.legacy, kind="Policy", embeddings={"policy_1": (0.4,)}
    )

    [entry] = merge_rig.logged()
    assert (entry.name, entry.identity) == ("Policy", "policy_1")


def test_the_builder_is_pure_and_keeps_only_ids_that_exist_with_a_null_embedding() -> None:
    primitives = build_embedding_backfill_primitives(
        {"a": True, "b": False},
        {"a": (1.0,), "b": (2.0,), "c": (3.0,)},
        label="Capability",
    )

    assert [(p.label, p.id, p.embedding) for p in primitives] == [("Capability", "a", (1.0,))]


def test_the_incoming_embedding_is_backfilled_on_the_baseline_graph_as_a_group_on_that_graph(
    merge_rig: MergeRig,
) -> None:
    baseline = merge_rig.graphs.open(BASELINE)
    baseline.seed_node(("Capability",), "capability_1", {"name": "c"})
    reader = SingleTenantStore(nodes=baseline.nodes)

    backfill_incoming_capability_embeddings(
        _target(merge_rig, BASELINE), reader, embeddings={"capability_1": (0.1, 0.2)}
    )

    [group] = [g for g in merge_rig.store.groups if g.graph == BASELINE]
    assert [(e.name, e.identity, e.embedding) for e in group.entries] == [
        ("Capability", "capability_1", (0.1, 0.2))
    ]
    assert baseline.nodes[("Capability", "capability_1")]["embedding"] == [0.1, 0.2]
    assert merge_rig.groups == []  # nothing on policy_system


def test_an_incoming_node_that_already_has_a_cached_embedding_is_not_rewritten(
    merge_rig: MergeRig,
) -> None:
    baseline = merge_rig.graphs.open(BASELINE)
    baseline.seed_node(("Capability",), "capability_1", {"name": "c", "embedding": [0.9]})
    reader = SingleTenantStore(nodes=baseline.nodes)

    backfill_incoming_capability_embeddings(
        _target(merge_rig, BASELINE), reader, embeddings={"capability_1": (0.1, 0.2)}
    )

    assert [g for g in merge_rig.store.groups if g.graph == BASELINE] == []
    assert cast("list[float]", baseline.nodes[("Capability", "capability_1")]["embedding"]) == [0.9]
