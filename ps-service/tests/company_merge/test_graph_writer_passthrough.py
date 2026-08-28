"""Tests for `ps_service.company_merge.graph_writer.
persist_role_and_requirement_passthrough` (PLAN_REVIEWED.md §10 Increment
10): the unconditional-`SET` Regulation/Role/Requirement writer, mirroring
#15's own `persist_role_and_requirement_graph` shape exactly.

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.company_merge.falkordb_client`) structurally -- no mocking
library, matching L2 Testing Patterns' "mock at component boundaries" and
this issue's binding testing convention.
"""

from __future__ import annotations

from dataclasses import dataclass

from ps_service.company_merge.graph_writer import (
    persist_role_and_requirement_passthrough,
)
from ps_service.company_merge.models import BaselineNode, ProvenanceEdge


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
    """Satisfies `GraphHandle` structurally, capturing every `(query,
    params)` call for assertion."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


def _role_node() -> BaselineNode:
    return BaselineNode(id="role_manufacturer_abc123", properties={"name": "Manufacturer", "confidence": 0.9})


def _requirement_node() -> BaselineNode:
    return BaselineNode(
        id="CRA-1.0_req_art_13.1",
        properties={
            "text": "Conduct a cybersecurity risk assessment.",
            "type": "requirement",
            "confidence": 0.9,
            "role_id": "role_manufacturer_abc123",
        },
    )


def _regulation_properties() -> dict[str, object]:
    return {"title": "Cyber Resilience Act", "jurisdiction": "EU"}


def test_persist_writes_regulation_role_and_requirement_with_edges() -> None:
    graph = _FakeGraph()
    role = _role_node()
    requirement = _requirement_node()
    defines_edge = ProvenanceEdge(
        relationship_type="DEFINES", target_id=role.id, source_ref="Art. 13(1)"
    )
    expresses_edge = ProvenanceEdge(
        relationship_type="EXPRESSES", target_id=requirement.id, source_ref="Art. 13(1)"
    )

    persist_role_and_requirement_passthrough(
        graph,
        "CRA-1.0",
        _regulation_properties(),
        (role,),
        (requirement,),
        (defines_edge, expresses_edge),
    )

    assert len(graph.calls) == 5
    regulation_call, role_call, requirement_call, defines_call, expresses_call = graph.calls

    assert regulation_call.query == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"
    assert regulation_call.params == {"id": "CRA-1.0", "properties": _regulation_properties()}

    assert role_call.query == "MERGE (n:Role {id: $id}) SET n += $properties"
    assert role_call.params == {"id": role.id, "properties": role.properties}

    assert requirement_call.query == "MERGE (n:Requirement {id: $id}) SET n += $properties"
    assert requirement_call.params == {"id": requirement.id, "properties": requirement.properties}

    assert defines_call.query == (
        "MATCH (r:RegulatoryInstrument {id: $regulation_id}), (n:Role {id: $target_id}) "
        "MERGE (r)-[e:DEFINES]->(n) SET e.source_ref = $source_ref"
    )
    assert defines_call.params == {
        "regulation_id": "CRA-1.0",
        "target_id": role.id,
        "source_ref": "Art. 13(1)",
    }

    assert expresses_call.query == (
        "MATCH (r:RegulatoryInstrument {id: $regulation_id}), (n:Requirement {id: $target_id}) "
        "MERGE (r)-[e:EXPRESSES]->(n) SET e.source_ref = $source_ref"
    )
    assert expresses_call.params == {
        "regulation_id": "CRA-1.0",
        "target_id": requirement.id,
        "source_ref": "Art. 13(1)",
    }


def test_persist_is_idempotent_across_repeated_calls() -> None:
    """Re-running with identical input twice against the same fake graph
    produces the same two sets of calls each time (trivial idempotency
    shape check -- PLAN_REVIEWED.md §10 Increment 10)."""
    graph = _FakeGraph()
    role = _role_node()
    requirement = _requirement_node()
    defines_edge = ProvenanceEdge(
        relationship_type="DEFINES", target_id=role.id, source_ref="Art. 13(1)"
    )
    expresses_edge = ProvenanceEdge(
        relationship_type="EXPRESSES", target_id=requirement.id, source_ref="Art. 13(1)"
    )

    for _ in range(2):
        persist_role_and_requirement_passthrough(
            graph,
            "CRA-1.0",
            _regulation_properties(),
            (role,),
            (requirement,),
            (defines_edge, expresses_edge),
        )

    assert len(graph.calls) == 10
    first_run, second_run = graph.calls[:5], graph.calls[5:]
    assert first_run == second_run


def test_persist_with_no_role_or_requirement_nodes_writes_only_regulation() -> None:
    graph = _FakeGraph()

    persist_role_and_requirement_passthrough(
        graph, "CRA-1.0", _regulation_properties(), (), (), ()
    )

    assert len(graph.calls) == 1
    assert graph.calls[0].query == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"
