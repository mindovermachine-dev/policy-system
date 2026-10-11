"""Tests for `graph_reader.read_baseline_graph`'s governance read (issues #183, #227).

Restore of a curated internal instrument and a live internal ingest both read
the whole authored Policy/Standard/Control tree regardless of status, and carry
every node property so the imported draft is the full authored content a
reviewer will be asked to approve. There is no approved-only read any more.
"""

from __future__ import annotations

from dataclasses import dataclass

from ps_service.company_merge.graph_reader import read_baseline_graph


@dataclass
class _FakeNode:
    properties: dict[str, object]


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeBaselineGraph:
    """Answers the governance-node reads; every other `read_baseline_graph` query is empty."""

    def __init__(self, nodes_by_label: dict[str, list[dict[str, object]]]) -> None:
        self._nodes_by_label = nodes_by_label
        self.queries: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        del params
        self.queries.append(q)
        for label, nodes in self._nodes_by_label.items():
            if q == f"MATCH (n:{label}) RETURN n":
                return _FakeQueryResult([[_FakeNode(dict(node))] for node in nodes])
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult([[_FakeNode({"id": "ENGPRAC-1.0"})]])
        return _FakeQueryResult([])


def _baseline() -> _FakeBaselineGraph:
    return _FakeBaselineGraph(
        {
            "Policy": [
                {
                    "id": "pol_a",
                    "title": "Access Policy",
                    "status": "draft",
                    "version": "1",
                    "scope_in": "all repos",
                    "embedding": [0.1, 0.2],
                }
            ],
            "Standard": [{"id": "std_a", "title": "Access Standard", "procedure": "Do it"}],
            "Control": [
                {"id": "ctrl_a", "title": "Access Control", "type": "manual", "evidence_plan": "x"}
            ],
        }
    )


def test_draft_read_returns_non_approved_policy_standard_and_control() -> None:
    """AC-BI-004: a `draft` Policy and status-less Standard/Control are read, not dropped."""
    baseline = read_baseline_graph(_baseline(), "ENGPRAC-1.0")

    assert [node.id for node in baseline.policy_nodes] == ["pol_a"]
    assert [node.id for node in baseline.standard_nodes] == ["std_a"]
    assert [node.id for node in baseline.control_nodes] == ["ctrl_a"]


def test_draft_read_carries_every_authored_property() -> None:
    baseline = read_baseline_graph(_baseline(), "ENGPRAC-1.0")

    assert baseline.policy_nodes[0].properties["scope_in"] == "all repos"
    assert baseline.standard_nodes[0].properties["procedure"] == "Do it"
    assert baseline.control_nodes[0].properties["evidence_plan"] == "x"


def test_draft_read_leaves_id_and_embedding_out_of_properties() -> None:
    """`id` is the node identity; the embedding travels separately (offline artifact vectors)."""
    baseline = read_baseline_graph(_baseline(), "ENGPRAC-1.0")

    for node in (*baseline.policy_nodes, *baseline.standard_nodes, *baseline.control_nodes):
        assert "id" not in node.properties
        assert "embedding" not in node.properties


def test_read_never_filters_governance_nodes_on_approved_status() -> None:
    """AC-BI-012: no query restricts Policy/Standard/Control to `approved` any more."""
    graph = _baseline()

    read_baseline_graph(graph, "ENGPRAC-1.0")

    assert "MATCH (n:Policy) RETURN n" in graph.queries
    assert not any("n.status = 'approved'" in q for q in graph.queries)
