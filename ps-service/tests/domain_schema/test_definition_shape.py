"""Shape of `DOMAIN_SCHEMA` that no generated artifact can prove for itself (AC-BI-002)."""

from __future__ import annotations

from ps_service.domain_schema import DOMAIN_SCHEMA

_CLASSIFICATION_EDGES = ("COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY")
_POLICY_SUPERSEDED_BY = ("SUPERSEDED_BY", "Policy", "Policy")
_EXPECTED_EDGE_COUNT = 16
_EXPECTED_LABEL_COUNT = 10


def test_policy_superseded_by_edge_exists() -> None:
    """AC-BI-002 (D5): Policy -> Policy SUPERSEDED_BY is a schema edge, 0..1 : 0..1."""
    edges = {edge.key: edge for edge in DOMAIN_SCHEMA.edges}

    assert str(edges[_POLICY_SUPERSEDED_BY].cardinality) == "0..1 : 0..1"


def test_schema_has_sixteen_edges_and_ten_labels() -> None:
    """AC-BI-002: the full vocabulary, no duplicates by (type, source, target)."""
    assert len(DOMAIN_SCHEMA.edges) == _EXPECTED_EDGE_COUNT
    assert len(DOMAIN_SCHEMA.nodes) == _EXPECTED_LABEL_COUNT


def test_classification_edges_are_many_to_many() -> None:
    """D-F1: the four classification edges are 1..* : 0..*; inbound is 0..* : 1..*."""
    edges = [edge for edge in DOMAIN_SCHEMA.edges if edge.type in _CLASSIFICATION_EDGES]

    assert len(edges) == len(_CLASSIFICATION_EDGES)
    assert {str(edge.cardinality) for edge in edges} == {"1..* : 0..*"}
    assert {str(edge.cardinality.flipped()) for edge in edges} == {"0..* : 1..*"}


def test_instrument_superseded_by_note_documents_the_native_only_absorbed_flag() -> None:
    """#201: the native-graph `absorbed` edge flag is operational, outside the vocabulary."""
    [edge] = [
        e
        for e in DOMAIN_SCHEMA.edges
        if e.type == "SUPERSEDED_BY" and e.source == "RegulatoryInstrument"
    ]

    assert "absorbed" in edge.note
    assert all(prop.name != "absorbed" for prop in edge.properties)
