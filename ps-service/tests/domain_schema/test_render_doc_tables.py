"""Table renderers use the document's vocabulary for types, presence and cardinality."""

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
)
from ps_service.domain_schema.render_doc import (
    render_edge_catalog,
    render_properties_table,
    render_relationships_table,
)

_NODE = Node(
    label="Thing",
    properties=(
        Property("name", StringType(), Presence.REQUIRED),
        Property("when", DateType(), Presence.OPTIONAL, "A | pipe in a note."),
        Property("score", FloatRangeType(0.0, 1.0), Presence.REQUIRED, "Certainty."),
        Property("kind", EnumType(("a", "b")), Presence.CONDITIONAL, "Shapes identity."),
    ),
)


def test_properties_table_uses_the_documents_type_presence_and_note_vocabulary() -> None:
    assert render_properties_table(_NODE).splitlines() == [
        "| Property | Type | Required | Notes |",
        "|----------|------|----------|-------|",
        "| `name` | string | Yes | |",
        "| `when` | date (ISO 8601) | No | A \\| pipe in a note. |",
        "| `score` | float, 0.0\u20131.0 | Yes | Certainty. |",
        "| `kind` | enum: `a` \\| `b` | Conditional | Shapes identity. |",
    ]


def test_catalog_escapes_the_first_star_only_when_both_sides_have_one() -> None:
    schema = Schema(
        nodes=(Node("A", ()), Node("B", ())),
        edges=(
            Edge("X", "A", "B", Cardinality.parse("1..* : 0..*")),
            Edge("Y", "A", "B", Cardinality.parse("1 : 0..*")),
        ),
    )

    rows = render_edge_catalog(schema).splitlines()[2:]

    assert "| 1..\\* : 0..* |" in rows[0]
    assert "| 1 : 0..* |" in rows[1]


_SCHEMA = Schema(
    nodes=(Node("Alpha", ()), Node("Beta", ()), Node("PracticeArea", ())),
    edges=(
        Edge("OWNS", "PracticeArea", "Alpha", Cardinality.parse("1..* : 0..*"), note="Owns."),
        Edge(
            "LINKS",
            "Alpha",
            "Beta",
            Cardinality.parse("1 : 0..*"),
            properties=(EdgeProperty("source_ref", StringType(min_length=1), Presence.REQUIRED),),
            note="Links.",
        ),
        Edge("NEXT", "Alpha", "Alpha", Cardinality.parse("0..1 : 0..1"), note="Next."),
    ),
)


def test_relationships_table_lists_inbound_rows_first_then_outbound_with_suffixes() -> None:
    rows = render_relationships_table(_SCHEMA, "Alpha").splitlines()

    assert rows[:2] == [
        "| Edge | Target | Cardinality | Edge Properties | Note |",
        "|------|--------|-------------|------------------|------|",
    ]
    assert rows[2:] == [
        (
            "| `OWNS` (inbound) | PracticeArea | 0..\\* : 1..* | \u2014 "
            "| See [PracticeArea \u2192 OWNS](#practicearea). |"
        ),
        "| `NEXT` (inbound) | Alpha | 0..1 : 0..1 | \u2014 | See [Alpha \u2192 NEXT](#alpha). |",
        "| `LINKS` (outbound) | Beta | 1 : 0..* | `source_ref` (string, required) | Links. |",
        "| `NEXT` (outbound) | Alpha | 0..1 : 0..1 | \u2014 | Next. |",
    ]


def test_relationships_table_of_a_node_without_edges_has_only_the_header() -> None:
    schema = Schema(nodes=(Node("Lonely", ()),))

    assert len(render_relationships_table(schema, "Lonely").splitlines()) == 2
