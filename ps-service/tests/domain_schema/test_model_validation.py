"""Construction-time validation of the schema model (AC-BI-003)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from ps_service.domain_schema import (
    DOMAIN_SCHEMA,
    Cardinality,
    DomainSchemaError,
    Edge,
    EdgeProperty,
    EnumType,
    FloatRangeType,
    Multiplicity,
    Node,
    Presence,
    Property,
    Schema,
    StringType,
)
from ps_service.domain_schema.model import NOTE_MAX_CHARS

_NAME = Property("name", StringType(), Presence.REQUIRED)
_ONE_TO_MANY = Cardinality.parse("1 : 0..*")


def _node(label: str, *properties: Property) -> Node:
    return Node(label, properties or (_NAME,))


def _edge(edge_type: str, source: str, target: str) -> Edge:
    return Edge(edge_type, source, target, _ONE_TO_MANY)


def test_duplicate_label_raises_naming_the_label() -> None:
    """AC-BI-003: two nodes with the same label are rejected."""
    with pytest.raises(DomainSchemaError, match="Role"):
        Schema(nodes=(_node("Role"), _node("Role")))


def test_duplicate_edge_key_raises_naming_the_edge() -> None:
    """AC-BI-003: an edge repeated with the same (type, source, target) is rejected."""
    nodes = (_node("Role"), _node("Obligation"))
    edges = (_edge("HAS", "Role", "Obligation"), _edge("HAS", "Role", "Obligation"))

    with pytest.raises(DomainSchemaError, match="HAS"):
        Schema(nodes=nodes, edges=edges)


def test_same_edge_type_between_different_labels_is_allowed() -> None:
    """AC-BI-003: RI and Policy may both carry SUPERSEDED_BY; the key includes both ends."""
    nodes = (_node("RegulatoryInstrument"), _node("Policy"))
    edges = (
        _edge("SUPERSEDED_BY", "RegulatoryInstrument", "RegulatoryInstrument"),
        _edge("SUPERSEDED_BY", "Policy", "Policy"),
    )

    assert len(Schema(nodes=nodes, edges=edges).edges) == 2  # the two edges built above


@pytest.mark.parametrize(
    ("source", "target", "missing"),
    [("Ghost", "Role", "Ghost"), ("Role", "Phantom", "Phantom")],
)
def test_edge_naming_an_undefined_label_raises(source: str, target: str, missing: str) -> None:
    """AC-BI-003: an edge whose source or target label is not a node is rejected."""
    with pytest.raises(DomainSchemaError, match=missing):
        Schema(nodes=(_node("Role"),), edges=(_edge("HAS", source, target),))


def test_duplicate_property_name_raises() -> None:
    """AC-BI-003: a node may not declare the same property twice."""
    with pytest.raises(DomainSchemaError, match="title"):
        Node(
            "Role",
            (
                Property("title", StringType(), Presence.REQUIRED),
                Property("title", StringType(), Presence.OPTIONAL),
            ),
        )


def test_duplicate_edge_property_name_raises() -> None:
    """AC-BI-003: an edge may not declare the same property twice."""
    dup = (
        EdgeProperty("source_ref", StringType(), Presence.REQUIRED),
        EdgeProperty("source_ref", StringType(), Presence.OPTIONAL),
    )

    with pytest.raises(DomainSchemaError, match="source_ref"):
        Edge("DEFINES", "A", "B", _ONE_TO_MANY, properties=dup)


@pytest.mark.parametrize(
    ("values", "offender"),
    [((), "empty"), (("a", "b", "a"), "'a'")],
)
def test_bad_enum_values_raise(values: tuple[str, ...], offender: str) -> None:
    """AC-BI-003: an enum must be non-empty and free of duplicate values."""
    with pytest.raises(DomainSchemaError, match=offender):
        EnumType(values)


def test_float_range_with_minimum_above_maximum_raises() -> None:
    """AC-BI-003: a float range must have minimum <= maximum."""
    with pytest.raises(DomainSchemaError, match="range"):
        FloatRangeType(minimum=1.0, maximum=0.0)


def test_negative_string_min_length_raises() -> None:
    """AC-BI-003: a string min_length cannot be negative."""
    with pytest.raises(DomainSchemaError, match="min_length"):
        StringType(min_length=-1)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(-1, 1), (2, 1)],
)
def test_bad_multiplicity_raises(minimum: int, maximum: int) -> None:
    """AC-BI-003: a multiplicity needs 0 <= minimum <= maximum."""
    with pytest.raises(DomainSchemaError, match="multiplicity"):
        Multiplicity(minimum, maximum)


@pytest.mark.parametrize("bad_note", ["x" * (NOTE_MAX_CHARS + 1), "two\nlines"])
def test_property_note_too_long_or_multiline_raises(bad_note: str) -> None:
    """AC-BI-003: notes are one line of at most NOTE_MAX_CHARS characters."""
    with pytest.raises(DomainSchemaError, match="note"):
        Property("name", StringType(), Presence.REQUIRED, note=bad_note)


@pytest.mark.parametrize("bad_note", ["x" * (NOTE_MAX_CHARS + 1), "two\nlines"])
def test_node_edge_and_rule_notes_are_validated_too(bad_note: str) -> None:
    """AC-BI-003: the node note, edge note and provenance rule share the note limit."""
    with pytest.raises(DomainSchemaError, match="note"):
        Node("Role", (_NAME,), note=bad_note)
    with pytest.raises(DomainSchemaError, match="note"):
        Edge("HAS", "A", "B", _ONE_TO_MANY, note=bad_note)
    with pytest.raises(DomainSchemaError, match="provenance_rule"):
        Edge("HAS", "A", "B", _ONE_TO_MANY, provenance_rule=bad_note)


def test_note_at_the_limit_is_accepted() -> None:
    """AC-BI-003: the limit is inclusive."""
    assert Property("n", StringType(), Presence.OPTIONAL, note="x" * NOTE_MAX_CHARS)


@pytest.mark.parametrize("label", ["", "Role\n", "Ro le", "Role)-[:X]->(", "1Role", "Role;"])
def test_malformed_label_raises(label: str) -> None:
    """AC-BI-003: labels are interpolated into Cypher-adjacent tooling, so the shape is fixed."""
    with pytest.raises(DomainSchemaError, match="label"):
        Node(label, (_NAME,))


@pytest.mark.parametrize("edge_type", ["", "has", "HAS\n", "HAS]->(", "1HAS", "HAS-X"])
def test_malformed_edge_type_raises(edge_type: str) -> None:
    """AC-BI-003: edge types are upper snake case only."""
    with pytest.raises(DomainSchemaError, match="edge type"):
        Edge(edge_type, "A", "B", _ONE_TO_MANY)


def test_real_domain_schema_constructs() -> None:
    """AC-BI-003: the shipped DOMAIN_SCHEMA passes its own validation."""
    assert DOMAIN_SCHEMA.nodes


def test_domain_schema_imports_cleanly() -> None:
    """AC-BI-001: importing the package in a fresh interpreter succeeds and needs no file."""
    completed = subprocess.run(  # fixed argv, current interpreter, no user input
        [sys.executable, "-I", "-c", "import ps_service.domain_schema as m; m.DOMAIN_SCHEMA"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
