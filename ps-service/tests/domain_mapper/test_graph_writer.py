"""Tests for `ps_service.domain_mapper.graph_writer` (PLAN_REVIEWED.md §11
Increment 9).

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.domain_mapper.falkordb_client`) structurally — no mocking
library, matching L2 Testing Patterns' "mock at component boundaries" and
this issue's binding testing convention (§0.3/§0.5).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import redis.exceptions

from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.domain_mapper.errors import DomainMapperPersistenceError
from ps_service.domain_mapper.graph_writer import (
    persist_obligation_and_capability_graph,
    persist_role_and_requirement_graph,
)
from ps_service.domain_mapper.models import (
    CapabilityNode,
    CapabilityRequiresEdge,
    ObligationHasEdge,
    ObligationNode,
    RequirementExpressesEdge,
    RequirementNode,
    RequirementSatisfiedByEdge,
    RoleDefinesEdge,
    RoleNode,
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
    """Satisfies `GraphHandle` structurally, capturing every `(query,
    params)` call for assertion — this is what lets the B3 fix (a)
    regression test below assert `graph.calls == []` (zero writes) rather
    than merely "raised eventually"."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


class _FakeGraphThatRaisesConnectionError:
    """Satisfies `GraphHandle` structurally; every `query()` call raises
    `redis.exceptions.ConnectionError`."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        raise redis.exceptions.ConnectionError("Error 111 connecting to 127.0.0.1:6379")


def _role_node(role_id: str = "role_manufacturer_abc123", confidence: float = 0.9) -> RoleNode:
    return RoleNode(id=role_id, properties={"name": "Manufacturer", "confidence": confidence})


def _requirement_node(
    requirement_id: str = "CRA-1.0_req_art_13.1",
    role_id: str = "role_manufacturer_abc123",
    text: str = "Conduct a cybersecurity risk assessment.",
    confidence: float = 0.9,
) -> RequirementNode:
    return RequirementNode(
        id=requirement_id,
        properties={
            "text": text,
            "type": "requirement",
            "confidence": confidence,
            "role_id": role_id,
        },
    )


def _regulatory_instrument_properties() -> dict[str, object]:
    return {"title": "Cyber Resilience Act", "jurisdiction": "EU"}


# --- Exact Cypher shape, params-only properties -----------------------------


def test_persist_writes_regulation_role_and_requirement_with_edges() -> None:
    graph = _FakeGraph()
    role = _role_node()
    requirement = _requirement_node()
    role_edge = RoleDefinesEdge(role_node_id=role.id, source_ref="Art. 13(1)")
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=requirement.id, source_ref="Art. 13(1)"
    )

    persist_role_and_requirement_graph(
        graph,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (role,),
        (role_edge,),
        (requirement,),
        (requirement_edge,),
    )

    assert len(graph.calls) == 5
    regulatory_instrument_call, role_call, defines_call, requirement_call, expresses_call = graph.calls

    assert regulatory_instrument_call.query == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"
    assert regulatory_instrument_call.params == {"id": "CRA-1.0", "properties": _regulatory_instrument_properties()}

    assert role_call.query == "MERGE (n:Role {id: $id}) SET n += $properties"
    assert role_call.params == {"id": role.id, "properties": role.properties}

    assert defines_call.query == (
        "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), (n:Role {id: $target_id}) "
        "MERGE (r)-[e:DEFINES]->(n) SET e.source_ref = $source_ref"
    )
    assert defines_call.params == {
        "regulatory_instrument_id": "CRA-1.0",
        "target_id": role.id,
        "source_ref": "Art. 13(1)",
    }

    assert requirement_call.query == "MERGE (n:Requirement {id: $id}) SET n += $properties"
    assert requirement_call.params == {"id": requirement.id, "properties": requirement.properties}

    assert expresses_call.query == (
        "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), (n:Requirement {id: $target_id}) "
        "MERGE (r)-[e:EXPRESSES]->(n) SET e.source_ref = $source_ref"
    )
    assert expresses_call.params == {
        "regulatory_instrument_id": "CRA-1.0",
        "target_id": requirement.id,
        "source_ref": "Art. 13(1)",
    }


def test_persist_writes_instrument_type_verbatim_when_present_in_regulatory_instrument_properties() -> None:
    """AC-BI-010 (Domain Mapper, write side): `instrument_type` rides
    through the Regulation MERGE verbatim inside `params["properties"]` —
    `regulatory_instrument_properties` is `dict[str, object]` with no key allow-list,
    so the key propagates by construction with NO src change."""
    graph = _FakeGraph()

    persist_role_and_requirement_graph(
        graph,
        "NIS2-1.0",
        {"title": "NIS2 Directive", "jurisdiction": "EU", "instrument_type": "directive"},
        (),
        (),
        (),
        (),
    )

    assert len(graph.calls) == 1
    regulatory_instrument_call = graph.calls[0]
    assert regulatory_instrument_call.query == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"
    assert regulatory_instrument_call.params is not None
    properties = regulatory_instrument_call.params["properties"]
    assert isinstance(properties, dict)
    assert properties["instrument_type"] == "directive"


def test_persist_never_interpolates_source_ref_value_into_query_string() -> None:
    """`source_ref` is present in the DEFINES/EXPRESSES edge params, and its
    VALUE never appears in the query string itself — only the property key
    name `source_ref` does (a fixed literal, not the value)."""
    graph = _FakeGraph()
    role = _role_node()
    requirement = _requirement_node()
    distinctive_source_ref = "Art. 13(1)(zzz-distinctive-value)"
    role_edge = RoleDefinesEdge(role_node_id=role.id, source_ref=distinctive_source_ref)
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=requirement.id, source_ref=distinctive_source_ref
    )

    persist_role_and_requirement_graph(
        graph,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (role,),
        (role_edge,),
        (requirement,),
        (requirement_edge,),
    )

    for call in graph.calls:
        assert distinctive_source_ref not in call.query
    defines_call, expresses_call = graph.calls[2], graph.calls[4]
    assert defines_call.params is not None
    assert defines_call.params["source_ref"] == distinctive_source_ref
    assert expresses_call.params is not None
    assert expresses_call.params["source_ref"] == distinctive_source_ref


def test_persist_writes_zero_role_and_requirement_elements_when_collections_empty() -> None:
    """Zero-unit extraction: only the Regulation MERGE happens, no
    exception — mirrors Increment 10's zero-unit-extraction case."""
    graph = _FakeGraph()

    persist_role_and_requirement_graph(
        graph, "CRA-1.0", _regulatory_instrument_properties(), (), (), (), ()
    )

    assert len(graph.calls) == 1
    assert graph.calls[0].query == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"


# --- B3 fix (a): validate-then-write, zero writes before raise -------------


def test_persist_raises_before_any_write_when_requirement_role_id_is_dangling() -> None:
    """THE B3 fix (a) direct test. A Requirement node whose `role_id`
    references a Role NOT present in this same call's `role_nodes`
    collection -> DomainMapperPersistenceError raised, AND the fake graph's
    call log is asserted EMPTY at raise time — proving zero writes happened
    before the raise, not just that a raise happened."""
    graph = _FakeGraph()
    role = _role_node(role_id="role_manufacturer_abc123")
    dangling_requirement = _requirement_node(role_id="role_importer_does_not_exist")
    role_edge = RoleDefinesEdge(role_node_id=role.id, source_ref="Art. 13(1)")
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=dangling_requirement.id, source_ref="Art. 13(1)"
    )

    with pytest.raises(DomainMapperPersistenceError) as exc_info:
        persist_role_and_requirement_graph(
            graph,
            "CRA-1.0",
            _regulatory_instrument_properties(),
            (role,),
            (role_edge,),
            (dangling_requirement,),
            (requirement_edge,),
        )

    assert dangling_requirement.id in str(exc_info.value)
    assert "role_importer_does_not_exist" in str(exc_info.value)
    assert graph.calls == []


def test_persist_raises_when_role_nodes_collection_is_empty_but_requirement_references_role() -> (
    None
):
    """A second shape of the same dangling-reference bug: zero Role nodes
    at all in this call, but a Requirement still carries a role_id."""
    graph = _FakeGraph()
    requirement = _requirement_node(role_id="role_manufacturer_abc123")
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=requirement.id, source_ref="Art. 13(1)"
    )

    with pytest.raises(DomainMapperPersistenceError):
        persist_role_and_requirement_graph(
            graph,
            "CRA-1.0",
            _regulatory_instrument_properties(),
            (),
            (),
            (requirement,),
            (requirement_edge,),
        )

    assert graph.calls == []


# --- Dependency health wiring ------------------------------------------------


def test_persist_marks_falkordb_unhealthy_on_connection_error() -> None:
    graph = _FakeGraphThatRaisesConnectionError()

    with pytest.raises(DomainMapperPersistenceError):
        persist_role_and_requirement_graph(
            graph, "CRA-1.0", _regulatory_instrument_properties(), (), (), (), ()
        )

    assert is_healthy(FALKORDB) is False


def test_persist_marks_falkordb_healthy_on_success() -> None:
    graph = _FakeGraph()

    persist_role_and_requirement_graph(
        graph, "CRA-1.0", _regulatory_instrument_properties(), (), (), (), ()
    )

    assert is_healthy(FALKORDB) is True


def test_validation_error_does_not_mark_falkordb_unhealthy() -> None:
    """A B3-style validation failure (dangling role_id) never reaches
    `graph.query()` at all — it must not be mistaken for a FalkorDB
    outage."""
    graph = _FakeGraph()
    dangling_requirement = _requirement_node(role_id="role_does_not_exist")
    requirement_edge = RequirementExpressesEdge(
        requirement_node_id=dangling_requirement.id, source_ref="Art. 13(1)"
    )

    with pytest.raises(DomainMapperPersistenceError):
        persist_role_and_requirement_graph(
            graph,
            "CRA-1.0",
            _regulatory_instrument_properties(),
            (),
            (),
            (dangling_requirement,),
            (requirement_edge,),
        )

    assert is_healthy(FALKORDB) is True


# --- persist_obligation_and_capability_graph (PLAN_REVIEWED.md §11 Increment 15) --


def _obligation_node(
    obligation_id: str = "obl_cooperate_with_market_surveillance_requests_abc123",
    text: str = "Cooperate with market surveillance requests.",
    confidence: float = 0.85,
) -> ObligationNode:
    return ObligationNode(id=obligation_id, properties={"text": text, "confidence": confidence})


def _capability_node(
    capability_id: str = "cap_incident_reporting_process_def456",
    name: str = "Incident Reporting Process",
    confidence: float = 0.8,
    description: str | None = None,
) -> CapabilityNode:
    properties: dict[str, str | float] = {"name": name, "confidence": confidence}
    if description is not None:
        properties["description"] = description
    return CapabilityNode(id=capability_id, properties=properties)


def test_persist_writes_obligation_capability_nodes_and_edges_exact_shape() -> None:
    graph = _FakeGraph()
    obligation = _obligation_node()
    capability = _capability_node(description="Process for reporting security incidents.")
    has_edge = ObligationHasEdge(
        role_node_id="role_manufacturer_abc123", obligation_node_id=obligation.id
    )
    satisfied_by_edge = RequirementSatisfiedByEdge(
        requirement_id="CRA-1.0_req_art_13.1", obligation_node_id=obligation.id
    )
    requires_edge = CapabilityRequiresEdge(
        obligation_node_id=obligation.id, capability_node_id=capability.id
    )

    persist_obligation_and_capability_graph(
        graph, (obligation,), (has_edge,), (satisfied_by_edge,), (capability,), (requires_edge,)
    )

    assert len(graph.calls) == 5
    obligation_call, capability_call, has_call, satisfied_by_call, requires_call = graph.calls

    assert obligation_call.query == "MERGE (n:Obligation {id: $id}) SET n += $properties"
    assert obligation_call.params == {"id": obligation.id, "properties": obligation.properties}

    assert capability_call.query == "MERGE (n:Capability {id: $id}) SET n += $properties"
    assert capability_call.params == {"id": capability.id, "properties": capability.properties}

    assert has_call.query == (
        "MATCH (s:Role {id: $source_id}), (t:Obligation {id: $target_id}) "
        "MERGE (s)-[:HAS]->(t)"
    )
    assert has_call.params == {
        "source_id": has_edge.role_node_id,
        "target_id": has_edge.obligation_node_id,
    }

    assert satisfied_by_call.query == (
        "MATCH (s:Requirement {id: $source_id}), (t:Obligation {id: $target_id}) "
        "MERGE (s)-[:SATISFIED_BY]->(t)"
    )
    assert satisfied_by_call.params == {
        "source_id": satisfied_by_edge.requirement_id,
        "target_id": satisfied_by_edge.obligation_node_id,
    }

    assert requires_call.query == (
        "MATCH (s:Obligation {id: $source_id}), (t:Capability {id: $target_id}) "
        "MERGE (s)-[:REQUIRES]->(t)"
    )
    assert requires_call.params == {
        "source_id": requires_edge.obligation_node_id,
        "target_id": requires_edge.capability_node_id,
    }


def test_persist_obligation_and_capability_writes_zero_elements_when_collections_empty() -> None:
    graph = _FakeGraph()

    persist_obligation_and_capability_graph(graph, (), (), (), (), ())

    assert graph.calls == []


def test_persist_obligation_and_capability_writes_nodes_before_edges() -> None:
    """`MATCH`-based edge writes silently no-op against a not-yet-written
    node — write order is load-bearing, not stylistic. Assert both node
    MERGEs happen before either edge MATCH/MERGE call."""
    graph = _FakeGraph()
    obligation = _obligation_node()
    capability = _capability_node()
    has_edge = ObligationHasEdge(
        role_node_id="role_manufacturer_abc123", obligation_node_id=obligation.id
    )
    requires_edge = CapabilityRequiresEdge(
        obligation_node_id=obligation.id, capability_node_id=capability.id
    )

    persist_obligation_and_capability_graph(
        graph, (obligation,), (has_edge,), (), (capability,), (requires_edge,)
    )

    node_queries = [c.query for c in graph.calls if c.query.startswith("MERGE (n:")]
    edge_queries = [c.query for c in graph.calls if c.query.startswith("MATCH (s:")]
    assert len(node_queries) == 2
    assert len(edge_queries) == 2
    last_node_index = max(
        i for i, c in enumerate(graph.calls) if c.query.startswith("MERGE (n:")
    )
    first_edge_index = min(
        i for i, c in enumerate(graph.calls) if c.query.startswith("MATCH (s:")
    )
    assert last_node_index < first_edge_index


# --- Edge Catalog: HAS/SATISFIED_BY/REQUIRES carry NO properties, ever -----


def test_has_satisfied_by_requires_edges_never_carry_source_ref_or_any_property() -> None:
    """Direct test of the Edge Catalog's "no properties" column for these
    three edge types — an explicit negative assertion that the substring
    `"source_ref"` never appears in any HAS/SATISFIED_BY/REQUIRES query
    string or params dict, not just an absence-of-a-positive-check."""
    graph = _FakeGraph()
    obligation = _obligation_node()
    capability = _capability_node()
    has_edge = ObligationHasEdge(
        role_node_id="role_manufacturer_abc123", obligation_node_id=obligation.id
    )
    satisfied_by_edge = RequirementSatisfiedByEdge(
        requirement_id="CRA-1.0_req_art_13.1", obligation_node_id=obligation.id
    )
    requires_edge = CapabilityRequiresEdge(
        obligation_node_id=obligation.id, capability_node_id=capability.id
    )

    persist_obligation_and_capability_graph(
        graph, (obligation,), (has_edge,), (satisfied_by_edge,), (capability,), (requires_edge,)
    )

    edge_calls = [c for c in graph.calls if c.query.startswith("MATCH (s:")]
    assert len(edge_calls) == 3
    for call in edge_calls:
        assert "source_ref" not in call.query
        assert call.params is not None
        assert "source_ref" not in call.params
        assert "source_ref" not in repr(call.params)
        # No SET clause at all on any of these three edge types.
        assert "SET" not in call.query
        # Params carry only the two endpoint ids, nothing else.
        assert set(call.params.keys()) == {"source_id", "target_id"}


# --- Obligation node properties: text, confidence, params-only -------------


def test_obligation_node_properties_text_and_confidence_present_in_params_only() -> None:
    graph = _FakeGraph()
    obligation = _obligation_node(
        obligation_id="obl_report_security_incidents_xyz789",
        text="Report security incidents to the competent authority.",
        confidence=0.73,
    )

    persist_obligation_and_capability_graph(graph, (obligation,), (), (), (), ())

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert obligation.properties["text"] not in call.query
    assert call.params is not None
    properties = call.params["properties"]
    assert properties == {
        "text": "Report security incidents to the competent authority.",
        "confidence": 0.73,
    }


# --- Capability node properties: name, description?, confidence ------------


def test_capability_node_properties_include_description_when_present() -> None:
    graph = _FakeGraph()
    capability = _capability_node(
        capability_id="cap_incident_reporting_process_def456",
        name="Incident Reporting Process",
        confidence=0.9,
        description="A documented process for reporting security incidents.",
    )

    persist_obligation_and_capability_graph(graph, (), (), (), (capability,), ())

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.params is not None
    properties = call.params["properties"]
    assert properties == {
        "name": "Incident Reporting Process",
        "confidence": 0.9,
        "description": "A documented process for reporting security incidents.",
    }


def test_capability_node_omits_description_key_when_not_provided() -> None:
    graph = _FakeGraph()
    capability = _capability_node(
        capability_id="cap_incident_reporting_process_def456",
        name="Incident Reporting Process",
        confidence=0.9,
        description=None,
    )

    persist_obligation_and_capability_graph(graph, (), (), (), (capability,), ())

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.params is not None
    properties = call.params["properties"]
    assert "description" not in properties
    assert properties == {"name": "Incident Reporting Process", "confidence": 0.9}


# --- Dependency health wiring (shared _execute_query, sanity-checked here too) --


def test_persist_obligation_and_capability_marks_falkordb_unhealthy_on_connection_error() -> None:
    graph = _FakeGraphThatRaisesConnectionError()

    with pytest.raises(DomainMapperPersistenceError):
        persist_obligation_and_capability_graph(graph, (_obligation_node(),), (), (), (), ())

    assert is_healthy(FALKORDB) is False


def test_persist_obligation_and_capability_marks_falkordb_healthy_on_success() -> None:
    graph = _FakeGraph()

    persist_obligation_and_capability_graph(graph, (_obligation_node(),), (), (), (), ())

    assert is_healthy(FALKORDB) is True
