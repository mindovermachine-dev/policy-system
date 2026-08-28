"""Tests for `ps_service.company_merge.graph_reader` (PLAN_REVIEWED.md §10
Increment 5).

The fake `GraphHandle` dispatches by a distinctive substring of each query
-- mirroring `tests/domain_mapper/test_derivation.py`'s own
`_FakeBaselineGraph` dispatch style -- rather than importing this module's
private query constants, keeping the test decoupled from the exact query
string layout while still exercising each of the ten read-only queries
`read_baseline_graph` issues (module docstring's "six/seven" deviation
note).
"""

from __future__ import annotations

import pytest

from ps_service.company_merge.graph_reader import read_baseline_graph
from ps_service.company_merge.models import (
    BareEdge,
    BaselineGraph,
    BaselineNode,
    ProvenanceEdge,
)


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeRegulationNode:
    """Satisfies `graph_reader._RegulationNode` structurally -- only
    `.properties` is ever read."""

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _ScriptedFakeGraph:
    """Satisfies `GraphHandle` structurally. Every one of the ten queries
    `read_baseline_graph` issues is answered with its own scripted row set,
    dispatched by a distinctive substring of the query text."""

    def __init__(
        self,
        *,
        regulation_properties: dict[str, object],
        role_rows: list[object],
        requirement_rows: list[object],
        obligation_rows: list[object],
        capability_rows: list[object],
        defines_rows: list[object],
        expresses_rows: list[object],
        has_rows: list[object],
        satisfied_by_rows: list[object],
        requires_rows: list[object],
    ) -> None:
        self._regulation_properties = regulation_properties
        self._role_rows = role_rows
        self._requirement_rows = requirement_rows
        self._obligation_rows = obligation_rows
        self._capability_rows = capability_rows
        self._defines_rows = defines_rows
        self._expresses_rows = expresses_rows
        self._has_rows = has_rows
        self._satisfied_by_rows = satisfied_by_rows
        self._requires_rows = requires_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "[e:DEFINES]" in q:
            return _FakeQueryResult(self._defines_rows)
        if "[e:EXPRESSES]" in q:
            return _FakeQueryResult(self._expresses_rows)
        if "[:HAS]" in q:
            return _FakeQueryResult(self._has_rows)
        if "[:SATISFIED_BY]" in q:
            return _FakeQueryResult(self._satisfied_by_rows)
        if "[:REQUIRES]" in q:
            return _FakeQueryResult(self._requires_rows)
        if "n.role_id" in q:
            return _FakeQueryResult(self._requirement_rows)
        if "n.description" in q:
            return _FakeQueryResult(self._capability_rows)
        if "n.name, n.confidence" in q:
            return _FakeQueryResult(self._role_rows)
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult(self._obligation_rows)
        if "(n:RegulatoryInstrument {id: $regulation_id}) RETURN n" in q:
            return _FakeQueryResult([[_FakeRegulationNode(self._regulation_properties)]])
        raise AssertionError(f"unexpected query issued: {q!r}")


def test_read_baseline_graph_returns_exact_baseline_graph_for_full_data() -> None:
    graph = _ScriptedFakeGraph(
        regulation_properties={
            "id": "CRA-1.0",
            "title": "Cyber Resilience Act",
            "jurisdiction": "EU",
        },
        role_rows=[["role_manufacturer_abc123", "Manufacturer", 0.9]],
        requirement_rows=[
            [
                "CRA-1.0_req_art_13.1",
                "Conduct a cybersecurity risk assessment.",
                "requirement",
                0.9,
                "role_manufacturer_abc123",
            ]
        ],
        obligation_rows=[["obligation_abc123", "Conduct a risk assessment.", 0.85]],
        capability_rows=[
            [
                "capability_xyz789",
                "Risk Assessment Capability",
                0.8,
                "Ability to assess cybersecurity risk",
            ],
            ["capability_no_description", "Vulnerability Handling Capability", 0.75, None],
        ],
        defines_rows=[["role_manufacturer_abc123", "Article 13(1)"]],
        expresses_rows=[["CRA-1.0_req_art_13.1", "Article 13(1)"]],
        has_rows=[["role_manufacturer_abc123", "obligation_abc123"]],
        satisfied_by_rows=[["CRA-1.0_req_art_13.1", "obligation_abc123"]],
        requires_rows=[["obligation_abc123", "capability_xyz789"]],
    )

    result = read_baseline_graph(graph, "CRA-1.0")

    assert result == BaselineGraph(
        regulation_id="CRA-1.0",
        regulation_properties={
            "id": "CRA-1.0",
            "title": "Cyber Resilience Act",
            "jurisdiction": "EU",
        },
        role_nodes=(
            BaselineNode(
                id="role_manufacturer_abc123", properties={"name": "Manufacturer", "confidence": 0.9}
            ),
        ),
        requirement_nodes=(
            BaselineNode(
                id="CRA-1.0_req_art_13.1",
                properties={
                    "text": "Conduct a cybersecurity risk assessment.",
                    "type": "requirement",
                    "confidence": 0.9,
                    "role_id": "role_manufacturer_abc123",
                },
            ),
        ),
        obligation_nodes=(
            BaselineNode(
                id="obligation_abc123",
                properties={"text": "Conduct a risk assessment.", "confidence": 0.85},
            ),
        ),
        capability_nodes=(
            BaselineNode(
                id="capability_xyz789",
                properties={
                    "name": "Risk Assessment Capability",
                    "confidence": 0.8,
                    "description": "Ability to assess cybersecurity risk",
                },
            ),
            BaselineNode(
                id="capability_no_description",
                properties={"name": "Vulnerability Handling Capability", "confidence": 0.75},
            ),
        ),
        provenance_edges=(
            ProvenanceEdge(
                relationship_type="DEFINES",
                target_id="role_manufacturer_abc123",
                source_ref="Article 13(1)",
            ),
            ProvenanceEdge(
                relationship_type="EXPRESSES",
                target_id="CRA-1.0_req_art_13.1",
                source_ref="Article 13(1)",
            ),
        ),
        bare_edges=(
            BareEdge(
                relationship_type="HAS",
                source_id="role_manufacturer_abc123",
                target_id="obligation_abc123",
            ),
            BareEdge(
                relationship_type="SATISFIED_BY",
                source_id="CRA-1.0_req_art_13.1",
                target_id="obligation_abc123",
            ),
            BareEdge(
                relationship_type="REQUIRES",
                source_id="obligation_abc123",
                target_id="capability_xyz789",
            ),
        ),
    )


def test_read_baseline_graph_returns_empty_tuples_when_no_obligation_or_capability_nodes_exist() -> (
    None
):
    """AC-004 edge case (per #15's own DeriveObligationsAndCapabilities
    scope): a regulation whose derivation surfaced every Requirement as
    unmatched has zero Obligation and zero Capability nodes -- and
    therefore zero `HAS`/`SATISFIED_BY`/`REQUIRES` edges, since every one of
    those edge types requires an Obligation or Capability endpoint. Role/
    Requirement/`DEFINES`/`EXPRESSES` are still carried forward. No
    exception is raised."""
    graph = _ScriptedFakeGraph(
        regulation_properties={"id": "GDPR-1.0", "title": "GDPR", "jurisdiction": "EU"},
        role_rows=[["role_controller_def456", "Controller", 0.95]],
        requirement_rows=[
            ["GDPR-1.0_req_art_5.1", "Ensure lawful processing.", "requirement", 0.95, "role_controller_def456"]
        ],
        obligation_rows=[],
        capability_rows=[],
        defines_rows=[["role_controller_def456", "Article 5(1)"]],
        expresses_rows=[["GDPR-1.0_req_art_5.1", "Article 5(1)"]],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
    )

    result = read_baseline_graph(graph, "GDPR-1.0")

    assert result.obligation_nodes == ()
    assert result.capability_nodes == ()
    assert result.bare_edges == ()
    assert result.role_nodes != ()
    assert result.requirement_nodes != ()
    assert result.provenance_edges != ()


def test_read_regulation_properties_includes_instrument_type() -> None:
    """AC-BI-011 (Company Merge, read side): the Regulation property bag is
    read back whole (`dict(node.properties)`, no field filter), so
    `instrument_type` reaches `BaselineGraph.regulation_properties` with NO
    src change."""
    graph = _ScriptedFakeGraph(
        regulation_properties={
            "id": "NIS2-1.0",
            "title": "NIS2 Directive",
            "jurisdiction": "EU",
            "instrument_type": "directive",
        },
        role_rows=[],
        requirement_rows=[],
        obligation_rows=[],
        capability_rows=[],
        defines_rows=[],
        expresses_rows=[],
        has_rows=[],
        satisfied_by_rows=[],
        requires_rows=[],
    )

    result = read_baseline_graph(graph, "NIS2-1.0")

    assert result.regulation_properties["instrument_type"] == "directive"


@pytest.mark.falkordb_live
def test_read_baseline_graph_reads_a_real_persisted_baseline_graph_correctly() -> None:
    """Live smoke test: writes a minimal baseline graph via a direct Cypher
    call against a real, reachable FalkorDB instance, then asserts
    `read_baseline_graph` reads it back correctly. Requires FalkorDB running
    at 127.0.0.1:6379."""
    from ps_service.company_merge.falkordb_client import connect, select_graph

    db = connect(host="127.0.0.1", port=6379)
    graph = select_graph(db, "test_company_merge_graph_reader_live")

    graph.query(
        "MERGE (r:RegulatoryInstrument {id: $id}) SET r += $properties",
        params={"id": "LIVE-1.0", "properties": {"title": "Live Test Regulation"}},
    )
    graph.query(
        "MERGE (n:Role {id: $id}) SET n += $properties",
        params={"id": "role_live_abc", "properties": {"name": "LiveRole", "confidence": 0.9}},
    )
    graph.query(
        "MATCH (r:RegulatoryInstrument {id: $regulation_id}), (n:Role {id: $target_id}) "
        "MERGE (r)-[e:DEFINES]->(n) SET e.source_ref = $source_ref",
        params={
            "regulation_id": "LIVE-1.0",
            "target_id": "role_live_abc",
            "source_ref": "Article 1",
        },
    )

    result = read_baseline_graph(graph, "LIVE-1.0")

    assert result.regulation_properties["title"] == "Live Test Regulation"
    assert result.role_nodes == (
        BaselineNode(id="role_live_abc", properties={"name": "LiveRole", "confidence": 0.9}),
    )
    assert result.provenance_edges == (
        ProvenanceEdge(relationship_type="DEFINES", target_id="role_live_abc", source_ref="Article 1"),
    )
