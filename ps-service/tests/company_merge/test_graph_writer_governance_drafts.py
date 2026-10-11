"""Tests for `ps_service.company_merge.graph_writer.persist_governance_drafts` (issues #183, #210).

An internal instrument's Policy/Standard/Control tree is written into the single-tenant graph as
`draft`, owned by the importing caller, without ever converging onto an existing canonical node.
Since issue #210 the write is ONE Graph Write Gateway group: each node is a create-only
`UpsertNode` (an id the graph already holds is decided BEFORE submission, from one batched
existing-id read per label, and left out, so a node a user has since approved is never touched and
nothing is logged for it). The Policy `created_at` is drawn from the injected `ValueSource` once,
while the group is built, and a Policy embedding travels in the primitive's `embedding` field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from company_merge._gateway_fakes import POLICY_SYSTEM, stepped_values
from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_submission import GraphWriteTarget
from ps_service.company_merge.graph_writer import persist_governance_drafts
from ps_service.company_merge.models import BaselineNode, GovernanceDraftCounts

if TYPE_CHECKING:
    from company_merge._gateway_fakes import MergeRig
    from ps_service.graph_gateway.models import GraphLogEntry

_OWNER = ("alice@example.com", "https://idp.example/")
_FIRST_INSTANT = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def _policy(node_id: str = "pol_a", **extra: str | float) -> BaselineNode:
    return BaselineNode(
        id=node_id,
        properties={"title": "Access Control Policy", "status": "draft", "version": "1", **extra},
    )


def _standard(node_id: str = "std_a", **extra: str | float) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"title": "Access Standard", **extra})


def _control(node_id: str = "ctrl_a", **extra: str | float) -> BaselineNode:
    return BaselineNode(
        id=node_id, properties={"title": "Access Control", "type": "manual", **extra}
    )


def _persist(
    rig: MergeRig,
    policies: tuple[BaselineNode, ...],
    standards: tuple[BaselineNode, ...] = (),
    controls: tuple[BaselineNode, ...] = (),
    *,
    policy_embeddings: dict[str, tuple[float, ...]] | None = None,
) -> GovernanceDraftCounts:
    return persist_governance_drafts(
        GraphWriteTarget(rig.gateway, POLICY_SYSTEM, rig.audit_event_id),
        rig.legacy,
        policies,
        standards,
        controls,
        owner=_OWNER,
        policy_embeddings=policy_embeddings,
        values=stepped_values(),
    )


def _entries(rig: MergeRig) -> list[GraphLogEntry]:
    return [entry for group in rig.groups for entry in group.entries]


def _properties(entry: GraphLogEntry) -> dict[str, object]:
    properties = entry.content["properties"]
    assert isinstance(properties, dict)
    return {str(key): value for key, value in properties.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def test_drafts_are_one_group_with_status_owner_version_and_one_shared_created_at(
    merge_rig: MergeRig,
) -> None:
    """AC-BI-003/001/007: one group; Policy, Standard, Control in that order; one `created_at`."""
    _persist(
        merge_rig,
        (_policy("pol_a"), _policy("pol_b")),
        (_standard(),),
        (_control(),),
    )

    assert len(merge_rig.groups) == 1
    entries = _entries(merge_rig)
    assert [(e.name, e.identity) for e in entries] == [
        ("Policy", "pol_a"),
        ("Policy", "pol_b"),
        ("Standard", "std_a"),
        ("Control", "ctrl_a"),
    ]
    policy_a, policy_b, standard, control = (_properties(e) for e in entries)
    assert policy_a["created_at"] == policy_b["created_at"] == _FIRST_INSTANT.isoformat()
    assert policy_a["status"] == "draft"
    assert (policy_a["owner_subject"], policy_a["owner_issuer"]) == _OWNER
    assert policy_a["version"] == "1"
    assert "created_at" not in standard
    assert "created_at" not in control
    assert standard["status"] == control["status"] == "draft"


def test_standard_and_control_without_status_default_to_draft(merge_rig: MergeRig) -> None:
    """AC-BI-004: an artifact node with no `status` is written as `draft`."""
    _persist(merge_rig, (_policy(),), (_standard(),), (_control(),))

    assert "status" not in _standard().properties
    assert merge_rig.policy_system.nodes[("Standard", "std_a")]["status"] == "draft"
    assert merge_rig.policy_system.nodes[("Control", "ctrl_a")]["status"] == "draft"


def test_artifact_authored_status_is_overridden_and_counted_even_when_the_node_exists(
    merge_rig: MergeRig,
) -> None:
    """AC-BI-005: `approved`/`proposed` is written `draft` and counted, present or not."""
    merge_rig.policy_system.seed_node(("Standard",), "std_a", {"status": "approved"})

    counts = _persist(
        merge_rig,
        (_policy("pol_a", status="approved"),),
        (_standard("std_a", status="proposed"), _standard("std_b")),
        (_control("ctrl_a", status="approved"),),
    )

    nodes = merge_rig.policy_system.nodes
    assert nodes[("Policy", "pol_a")]["status"] == "draft"
    assert nodes[("Standard", "std_a")]["status"] == "approved"  # existing: untouched
    assert nodes[("Control", "ctrl_a")]["status"] == "draft"
    assert counts == GovernanceDraftCounts(policies=1, standards=2, controls=1, status_overridden=3)


def test_an_existing_approved_policy_is_neither_changed_nor_logged(merge_rig: MergeRig) -> None:
    """AC-BI-005/010: a node that already exists keeps status, owner and content; no entry."""
    merge_rig.policy_system.seed_node(
        ("Policy",),
        "pol_a",
        {"status": "approved", "owner_subject": "bob@example.com", "owner_issuer": "https://x/"},
    )
    merge_rig.policy_system.seed_node(("Standard",), "std_a", {"status": "approved"})
    merge_rig.policy_system.seed_node(("Control",), "ctrl_a", {"status": "approved"})

    _persist(merge_rig, (_policy(),), (_standard(),), (_control(),))

    nodes = merge_rig.policy_system.nodes
    assert nodes[("Policy", "pol_a")]["status"] == "approved"
    assert nodes[("Policy", "pol_a")]["owner_subject"] == "bob@example.com"
    assert nodes[("Standard", "std_a")]["status"] == "approved"
    assert nodes[("Control", "ctrl_a")]["status"] == "approved"
    assert merge_rig.groups == []


def test_the_existing_id_reads_are_one_batched_read_per_label(merge_rig: MergeRig) -> None:
    _persist(merge_rig, (_policy("pol_a"), _policy("pol_b")), (_standard(),), (_control(),))

    reads = [s for s in merge_rig.legacy.statements if s.startswith("MATCH")]
    assert reads == [
        "MATCH (n:Policy) WHERE n.id IN $ids RETURN n.id",
        "MATCH (n:Standard) WHERE n.id IN $ids RETURN n.id",
        "MATCH (n:Control) WHERE n.id IN $ids RETURN n.id",
    ]


def test_the_first_occurrence_of_a_repeated_id_wins(merge_rig: MergeRig) -> None:
    first = _policy("pol_a", title_note="first")
    second = _policy("pol_a", title_note="second")

    _persist(merge_rig, (first, second))

    assert [e.identity for e in _entries(merge_rig)] == ["pol_a"]
    assert merge_rig.policy_system.nodes[("Policy", "pol_a")]["title_note"] == "first"


def test_policy_embedding_travels_in_the_embedding_field_not_in_properties(
    merge_rig: MergeRig,
) -> None:
    """The offline embedding survives, so later Policy convergence on ingest can use it."""
    _persist(merge_rig, (_policy(),), policy_embeddings={"pol_a": (0.5, 0.25)})

    [entry] = _entries(merge_rig)
    assert entry.embedding == (0.5, 0.25)
    assert "embedding" not in _properties(entry)
    assert merge_rig.policy_system.nodes[("Policy", "pol_a")]["embedding"] == [0.5, 0.25]


def test_policy_without_an_embedding_gets_no_embedding_key(merge_rig: MergeRig) -> None:
    _persist(merge_rig, (_policy(),), policy_embeddings={})

    assert "embedding" not in merge_rig.policy_system.nodes[("Policy", "pol_a")]


def test_policy_without_version_defaults_to_string_one(merge_rig: MergeRig) -> None:
    """The lifecycle's `version` is string-typed and starts at `"1"`."""
    unversioned = BaselineNode(id="pol_a", properties={"title": "P", "status": "draft"})

    _persist(merge_rig, (unversioned,))

    assert merge_rig.policy_system.nodes[("Policy", "pol_a")]["version"] == "1"


def test_every_artifact_property_is_carried_onto_the_draft(merge_rig: MergeRig) -> None:
    """A reviewer approving the draft sees the full authored content, not a hollow shell."""
    _persist(
        merge_rig,
        (_policy(scope_in="all repos"),),
        (_standard(procedure="Do the thing"),),
        (_control(evidence_plan="Quarterly export"),),
    )

    nodes = merge_rig.policy_system.nodes
    assert nodes[("Policy", "pol_a")]["scope_in"] == "all repos"
    assert nodes[("Standard", "std_a")]["procedure"] == "Do the thing"
    assert nodes[("Control", "ctrl_a")]["evidence_plan"] == "Quarterly export"


def test_counts_describe_the_input_not_the_nodes_actually_created(merge_rig: MergeRig) -> None:
    merge_rig.policy_system.seed_node(("Policy",), "pol_a", {"status": "approved"})

    counts = _persist(merge_rig, (_policy("pol_a"), _policy("pol_b")), (_standard(),))

    assert counts == GovernanceDraftCounts(policies=2, standards=1, controls=0, status_overridden=0)
    assert [e.identity for e in _entries(merge_rig)] == ["pol_b", "std_a"]


def test_second_restore_adds_no_nodes_and_logs_nothing(merge_rig: MergeRig) -> None:
    """AC-BI-011: repeating the same import leaves the same node set; the second group is empty."""
    args = ((_policy(),), (_standard(),), (_control(),))

    _persist(merge_rig, *args)
    first = {key: dict(value) for key, value in merge_rig.policy_system.nodes.items()}
    logged = len(merge_rig.logged())
    _persist(merge_rig, *args)

    assert merge_rig.policy_system.nodes == first
    assert len(merge_rig.logged()) == logged


def test_empty_input_issues_no_queries_submits_nothing_and_counts_zero(
    merge_rig: MergeRig,
) -> None:
    """AC-BI-007: an external-sourced import (no governance content) writes nothing."""
    counts = _persist(merge_rig, ())

    assert merge_rig.legacy.statements == []
    assert merge_rig.groups == []
    assert counts == GovernanceDraftCounts(policies=0, standards=0, controls=0, status_overridden=0)


def test_a_property_the_graph_cannot_store_raises_before_anything_is_submitted(
    merge_rig: MergeRig,
) -> None:
    bad = BaselineNode(id="pol_bad", properties={"title": "x", "score": float("inf")})

    with pytest.raises(CompanyMergePersistenceError, match="cannot store"):
        _persist(merge_rig, (_policy(), bad))

    assert merge_rig.groups == []
