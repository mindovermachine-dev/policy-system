"""Tests for `ps_service.company_merge.graph_writer.persist_governance_drafts` (issue #183).

Restore of an internal curated instrument writes its Policy/Standard/Control
tree into the single-tenant graph as `draft`, owned by the restoring caller,
without ever converging onto an existing canonical node. Every write is
`MERGE ... ON CREATE SET`, so a node that already exists -- including one a
user has since approved -- is never touched by a re-restore.

The fake implements FalkorDB's `MERGE ... ON CREATE SET` semantics for real
(rather than only recording calls) so idempotency is proven against behaviour,
not against the Cypher text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from ps_service.company_merge.graph_writer import persist_governance_drafts
from ps_service.company_merge.models import BaselineNode, GovernanceDraftCounts

_OWNER = ("alice@example.com", "https://idp.example/")
_UPSERT = re.compile(r"^MERGE \(n:(?P<label>\w+) \{id: \$id\}\) ON CREATE SET n \+= \$properties$")


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    @property
    def result_set(self) -> list[object]:
        return []


class _StatefulFakeGraph:
    """Records every call and keeps nodes keyed by `(label, id)`, honouring `ON CREATE SET`."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []
        self.nodes: dict[tuple[str, str], dict[str, object]] = {}

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        match = _UPSERT.match(q)
        assert match is not None, f"unexpected query shape: {q!r}"
        assert params is not None
        key = (match["label"], cast("str", params["id"]))
        if key not in self.nodes:
            self.nodes[key] = dict(cast("dict[str, object]", params["properties"]))
        return _FakeQueryResult()


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


def test_policy_standard_and_control_are_written_as_draft() -> None:
    """AC-BI-003: every restored Policy, Standard and Control is `status = draft`."""
    graph = _StatefulFakeGraph()

    persist_governance_drafts(
        graph, (_policy(),), (_standard(),), (_control(),), owner=_OWNER, policy_embeddings=None
    )

    assert graph.nodes[("Policy", "pol_a")]["status"] == "draft"
    assert graph.nodes[("Standard", "std_a")]["status"] == "draft"
    assert graph.nodes[("Control", "ctrl_a")]["status"] == "draft"


def test_standard_and_control_without_status_default_to_draft() -> None:
    """AC-BI-004: an artifact node with no `status` is written as `draft`."""
    graph = _StatefulFakeGraph()

    persist_governance_drafts(
        graph, (_policy(),), (_standard(),), (_control(),), owner=_OWNER, policy_embeddings=None
    )

    assert "status" not in _standard().properties
    assert graph.nodes[("Standard", "std_a")]["status"] == "draft"
    assert graph.nodes[("Control", "ctrl_a")]["status"] == "draft"


def test_non_draft_artifact_status_is_overridden_and_counted() -> None:
    """AC-BI-005: an artifact `approved`/`proposed` status is written as `draft` and counted."""
    graph = _StatefulFakeGraph()

    counts = persist_governance_drafts(
        graph,
        (_policy("pol_a", status="approved"),),
        (_standard("std_a", status="proposed"), _standard("std_b")),
        (_control("ctrl_a", status="approved"),),
        owner=_OWNER,
        policy_embeddings=None,
    )

    assert graph.nodes[("Policy", "pol_a")]["status"] == "draft"
    assert graph.nodes[("Standard", "std_a")]["status"] == "draft"
    assert graph.nodes[("Control", "ctrl_a")]["status"] == "draft"
    assert counts == GovernanceDraftCounts(policies=1, standards=2, controls=1, status_overridden=3)


def test_policy_is_owned_by_the_restoring_caller() -> None:
    """AC-BI-001: `owner_subject`/`owner_issuer` are the caller's `(sub, iss)` pair."""
    graph = _StatefulFakeGraph()

    persist_governance_drafts(graph, (_policy(),), (), (), owner=_OWNER, policy_embeddings=None)

    policy = graph.nodes[("Policy", "pol_a")]
    assert policy["owner_subject"] == "alice@example.com"
    assert policy["owner_issuer"] == "https://idp.example/"


def test_policy_without_version_defaults_to_string_one() -> None:
    """The lifecycle's `version` is string-typed and starts at `"1"`."""
    graph = _StatefulFakeGraph()
    unversioned = BaselineNode(id="pol_a", properties={"title": "P", "status": "draft"})

    persist_governance_drafts(graph, (unversioned,), (), (), owner=_OWNER, policy_embeddings=None)

    assert graph.nodes[("Policy", "pol_a")]["version"] == "1"


def test_every_artifact_property_is_carried_onto_the_draft() -> None:
    """A reviewer approving the draft sees the full authored content, not a hollow shell."""
    graph = _StatefulFakeGraph()

    persist_governance_drafts(
        graph,
        (_policy(scope_in="all repos"),),
        (_standard(procedure="Do the thing"),),
        (_control(evidence_plan="Quarterly export"),),
        owner=_OWNER,
        policy_embeddings=None,
    )

    assert graph.nodes[("Policy", "pol_a")]["scope_in"] == "all repos"
    assert graph.nodes[("Standard", "std_a")]["procedure"] == "Do the thing"
    assert graph.nodes[("Control", "ctrl_a")]["evidence_plan"] == "Quarterly export"


def test_artifact_supplied_policy_embedding_is_stored() -> None:
    """The offline embedding survives, so later Policy convergence on ingest can use it."""
    graph = _StatefulFakeGraph()

    persist_governance_drafts(
        graph, (_policy(),), (), (), owner=_OWNER, policy_embeddings={"pol_a": (0.5, 0.25)}
    )

    assert graph.nodes[("Policy", "pol_a")]["embedding"] == [0.5, 0.25]


def test_policy_without_an_embedding_gets_no_embedding_key() -> None:
    graph = _StatefulFakeGraph()

    persist_governance_drafts(graph, (_policy(),), (), (), owner=_OWNER, policy_embeddings={})

    assert "embedding" not in graph.nodes[("Policy", "pol_a")]


def test_existing_approved_tree_is_not_reset_by_a_second_restore() -> None:
    """AC-BI-010: `ON CREATE SET` leaves an approved node's status and owner alone."""
    graph = _StatefulFakeGraph()
    graph.nodes[("Policy", "pol_a")] = {
        "status": "approved",
        "owner_subject": "bob@example.com",
        "owner_issuer": "https://idp.example/",
    }
    graph.nodes[("Standard", "std_a")] = {"status": "approved"}
    graph.nodes[("Control", "ctrl_a")] = {"status": "approved"}

    persist_governance_drafts(
        graph, (_policy(),), (_standard(),), (_control(),), owner=_OWNER, policy_embeddings=None
    )

    assert graph.nodes[("Policy", "pol_a")]["status"] == "approved"
    assert graph.nodes[("Policy", "pol_a")]["owner_subject"] == "bob@example.com"
    assert graph.nodes[("Standard", "std_a")]["status"] == "approved"
    assert graph.nodes[("Control", "ctrl_a")]["status"] == "approved"


def test_second_restore_adds_no_nodes() -> None:
    """AC-BI-011: repeating the same restore leaves the same node set."""
    graph = _StatefulFakeGraph()
    args = ((_policy(),), (_standard(),), (_control(),))

    persist_governance_drafts(graph, *args, owner=_OWNER, policy_embeddings=None)
    first = {key: dict(value) for key, value in graph.nodes.items()}
    persist_governance_drafts(graph, *args, owner=_OWNER, policy_embeddings=None)

    assert graph.nodes == first


def test_empty_input_issues_no_queries_and_counts_zero() -> None:
    """AC-BI-007: an external-sourced restore (no governance content) writes nothing."""
    graph = _StatefulFakeGraph()

    counts = persist_governance_drafts(graph, (), (), (), owner=_OWNER, policy_embeddings=None)

    assert graph.calls == []
    assert counts == GovernanceDraftCounts(policies=0, standards=0, controls=0, status_overridden=0)
