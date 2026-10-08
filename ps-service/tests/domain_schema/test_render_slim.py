"""Slim renderer behaviour on small, locally built schemas (AC-BI-002, AC-BI-009)."""

from __future__ import annotations

from ps_service.domain_schema import (
    Cardinality,
    DateType,
    Edge,
    EdgeProperty,
    EnumType,
    FloatRangeType,
    Node,
    Presence,
    Property,
    Schema,
    StringType,
    render_slim_schema,
)

_RI = Node("RegulatoryInstrument", (Property("id", StringType(), Presence.REQUIRED),))
_ROLE = Node("Role", (Property("name", StringType(), Presence.REQUIRED),))


def test_edge_shows_direction_cardinality_and_edge_property() -> None:
    """AC-BI-002/009: an edge renders source, type, edge property, target and cardinality."""
    defines = Edge(
        type="DEFINES",
        source="RegulatoryInstrument",
        target="Role",
        cardinality=Cardinality.parse("1 : 0..*"),
        properties=(EdgeProperty("source_ref", StringType(min_length=1), Presence.REQUIRED),),
    )

    text = render_slim_schema(Schema(nodes=(_RI, _ROLE), edges=(defines,)))

    assert "(:RegulatoryInstrument)-[:DEFINES {source_ref: string !}]->(:Role)  1 : 0..*\n" in text
    assert "\nEDGES\n" in text


def test_edge_without_properties_has_no_braces() -> None:
    """AC-BI-002: an edge with no edge properties renders a bare relationship type."""
    merged = Edge(
        type="MERGED_INTO",
        source="Role",
        target="Role",
        cardinality=Cardinality.parse("0..1 : 0..*"),
    )

    text = render_slim_schema(Schema(nodes=(_ROLE,), edges=(merged,)))

    assert "(:Role)-[:MERGED_INTO]->(:Role)  0..1 : 0..*\n" in text


def test_property_types_and_presence_marks_render_compactly() -> None:
    """AC-BI-009: type text, enum values and the required/conditional/optional marks."""
    node = Node(
        "Thing",
        (
            Property("kind", EnumType(("a", "b")), Presence.CONDITIONAL),
            Property("when", DateType(), Presence.OPTIONAL),
            Property("score", FloatRangeType(0.0, 1.0), Presence.REQUIRED),
        ),
    )

    text = render_slim_schema(Schema(nodes=(node,)))

    assert "  kind: enum(a|b) ?\n" in text
    assert "  when: date\n" in text
    assert "  score: float[0.0..1.0] !\n" in text
