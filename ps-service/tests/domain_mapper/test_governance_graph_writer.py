"""Tests for `ps_service.domain_mapper.graph_writer.persist_governance_graph` (issue #54, S3).

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.domain_mapper.falkordb_client`) structurally -- no mocking
library, matching this issue's binding testing convention. Mirrors
`test_graph_writer.py`'s own style for `persist_obligation_and_capability_graph`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ps_service.domain_mapper.graph_writer import persist_governance_graph
from ps_service.domain_mapper.models import (
    ControlNode,
    PolicyGovernedByEdge,
    PolicyNode,
    PolicySupportedByEdge,
    StandardImplementedByEdge,
    StandardNode,
)


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    """Satisfies `GraphHandle` structurally, capturing every `(query, params)` call."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


def _policy_node(
    policy_node_id: str = "pol_data_protection_a8f3b1", title: str = "Data Protection Policy"
) -> PolicyNode:
    return PolicyNode(id=policy_node_id, properties={"title": title, "status": "draft"})


def _standard_node(
    standard_node_id: str = "std_pol_data_protection_a8f3b1_v1",
    title: str = "Security Log Retention Standard",
) -> StandardNode:
    return StandardNode(
        id=standard_node_id, properties={"title": title, "implementation_status": "draft"}
    )


def _control_node(
    control_node_id: str = "ctrl_std_pol_data_protection_a8f3b1_v1_automated",
    control_type: str = "automated",
    title: str = "Automated Log Retention Integrity Check",
) -> ControlNode:
    return ControlNode(
        id=control_node_id,
        properties={"type": control_type, "title": title, "implementation_status": "planned"},
    )


def test_persist_writes_policy_standard_control_nodes_and_edges_exact_shape() -> None:
    graph = _FakeGraph()
    policy = _policy_node()
    standard = _standard_node()
    control = _control_node()
    governed_by_edge = PolicyGovernedByEdge(
        capability_node_id="cap_security_logging_abc123", policy_node_id=policy.id
    )
    supported_by_edge = PolicySupportedByEdge(
        policy_node_id=policy.id, standard_node_id=standard.id
    )
    implemented_by_edge = StandardImplementedByEdge(
        standard_node_id=standard.id, control_node_id=control.id
    )

    persist_governance_graph(
        graph,
        (policy,),
        (governed_by_edge,),
        (standard,),
        (supported_by_edge,),
        (control,),
        (implemented_by_edge,),
    )

    assert len(graph.calls) == 6
    (
        policy_call,
        standard_call,
        control_call,
        governed_by_call,
        supported_by_call,
        implemented_by_call,
    ) = graph.calls

    assert policy_call.query == "MERGE (n:Policy {id: $id}) SET n += $properties"
    assert policy_call.params == {"id": policy.id, "properties": policy.properties}

    assert standard_call.query == "MERGE (n:Standard {id: $id}) SET n += $properties"
    assert standard_call.params == {"id": standard.id, "properties": standard.properties}

    assert control_call.query == "MERGE (n:Control {id: $id}) SET n += $properties"
    assert control_call.params == {"id": control.id, "properties": control.properties}

    assert governed_by_call.query == (
        "MATCH (s:Capability {id: $source_id}), (t:Policy {id: $target_id}) "
        "MERGE (s)-[:GOVERNED_BY]->(t)"
    )
    assert governed_by_call.params == {
        "source_id": governed_by_edge.capability_node_id,
        "target_id": governed_by_edge.policy_node_id,
    }

    assert supported_by_call.query == (
        "MATCH (s:Policy {id: $source_id}), (t:Standard {id: $target_id}) "
        "MERGE (s)-[:SUPPORTED_BY]->(t)"
    )
    assert supported_by_call.params == {
        "source_id": supported_by_edge.policy_node_id,
        "target_id": supported_by_edge.standard_node_id,
    }

    assert implemented_by_call.query == (
        "MATCH (s:Standard {id: $source_id}), (t:Control {id: $target_id}) "
        "MERGE (s)-[:IMPLEMENTED_BY]->(t)"
    )
    assert implemented_by_call.params == {
        "source_id": implemented_by_edge.standard_node_id,
        "target_id": implemented_by_edge.control_node_id,
    }


def test_persist_writes_zero_elements_when_collections_empty() -> None:
    graph = _FakeGraph()

    persist_governance_graph(graph, (), (), (), (), (), ())

    assert graph.calls == []


def test_persist_writes_nodes_before_edges() -> None:
    graph = _FakeGraph()
    policy = _policy_node()
    standard = _standard_node()
    governed_by_edge = PolicyGovernedByEdge(
        capability_node_id="cap_security_logging_abc123", policy_node_id=policy.id
    )
    supported_by_edge = PolicySupportedByEdge(
        policy_node_id=policy.id, standard_node_id=standard.id
    )

    persist_governance_graph(
        graph, (policy,), (governed_by_edge,), (standard,), (supported_by_edge,), (), ()
    )

    node_queries = [c.query for c in graph.calls if c.query.startswith("MERGE (n:")]
    edge_queries = [c.query for c in graph.calls if c.query.startswith("MATCH (s:")]
    assert len(node_queries) == 2
    assert len(edge_queries) == 2
    last_node_index = max(i for i, c in enumerate(graph.calls) if c.query.startswith("MERGE (n:"))
    first_edge_index = min(i for i, c in enumerate(graph.calls) if c.query.startswith("MATCH (s:"))
    assert last_node_index < first_edge_index


def test_governed_by_supported_by_implemented_by_edges_never_carry_a_property() -> None:
    """Edge Catalog: GOVERNED_BY/SUPPORTED_BY/IMPLEMENTED_BY carry NO properties, ever."""
    graph = _FakeGraph()
    policy = _policy_node()
    standard = _standard_node()
    control = _control_node()
    governed_by_edge = PolicyGovernedByEdge(
        capability_node_id="cap_security_logging_abc123", policy_node_id=policy.id
    )
    supported_by_edge = PolicySupportedByEdge(
        policy_node_id=policy.id, standard_node_id=standard.id
    )
    implemented_by_edge = StandardImplementedByEdge(
        standard_node_id=standard.id, control_node_id=control.id
    )

    persist_governance_graph(
        graph,
        (policy,),
        (governed_by_edge,),
        (standard,),
        (supported_by_edge,),
        (control,),
        (implemented_by_edge,),
    )

    edge_calls = [c for c in graph.calls if c.query.startswith("MATCH (s:")]
    assert len(edge_calls) == 3
    for call in edge_calls:
        assert "SET" not in call.query
        assert call.params is not None
        assert set(call.params.keys()) == {"source_id", "target_id"}


# --- AC-BI-017: Control operational fields left null on mint ----------------


def test_control_operational_fields_left_null_on_mint() -> None:
    """A `ControlNode` built without `execution_frequency`/`last_test_date`/
    `next_review_date`/`evidence_ref` (as `governance.py::_to_control_node`
    always builds one, per AC-BI-017) is persisted with exactly the
    properties it was given -- `persist_governance_graph` never adds a
    default or a literal `null` for any of the four operational fields; they
    are simply absent from the write.
    """
    graph = _FakeGraph()
    control = ControlNode(
        id="ctrl_std_pol_data_protection_a8f3b1_v1_automated",
        properties={
            "type": "automated",
            "title": "Automated Log Retention Integrity Check",
            "implementation_status": "planned",
            "confidence": 0.79,
        },
    )

    persist_governance_graph(graph, (), (), (), (), (control,), ())

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == "MERGE (n:Control {id: $id}) SET n += $properties"
    assert call.params is not None
    properties = call.params["properties"]
    assert isinstance(properties, dict)
    for operational_field in (
        "execution_frequency",
        "last_test_date",
        "next_review_date",
        "evidence_ref",
    ):
        assert operational_field not in properties
    assert properties == {
        "type": "automated",
        "title": "Automated Log Retention Integrity Check",
        "implementation_status": "planned",
        "confidence": 0.79,
    }
