"""Render the schema as the slim plain-text form served by the `domain_concepts` tool.

No notes and no prose: only labels, properties (type and presence) and,
edges.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.domain_schema.model import (
    ConstType,
    DateType,
    EnumType,
    FloatRangeType,
    Presence,
    StringType,
)

if TYPE_CHECKING:
    from ps_service.domain_schema.model import (
        Edge,
        EdgeProperty,
        Node,
        Property,
        PropertyType,
        Schema,
    )

LEGEND = (
    'Capability status "merged" is a tombstone (see MERGED_INTO to its survivor); '
    'readers filter status = "active" unless they want tombstones.'
)
_HEADER = (
    "PS domain schema (generated from code; prose lives in the psdomain://concepts resource)\n"
    "Every node has a string id. Property marks: ! required, ? conditional, none optional.\n"
    'Cardinality "S : T": S source nodes per target node, T target nodes per source node.\n'
    f"{LEGEND}\n"
)
_PRESENCE_MARK = {
    Presence.REQUIRED: " !",
    Presence.CONDITIONAL: " ?",
    Presence.OPTIONAL: "",
}


def _render_type(property_type: PropertyType) -> str:
    match property_type:
        case StringType():
            return "string"
        case DateType():
            return "date"
        case FloatRangeType(minimum=low, maximum=high):
            return f"float[{low}..{high}]"
        case EnumType(values=values):
            return f"enum({'|'.join(values)})"
        case ConstType(value=value):
            return f"const({value})"


def _render_field(name: str, field_type: PropertyType, presence: Presence) -> str:
    return f"{name}: {_render_type(field_type)}{_PRESENCE_MARK[presence]}"


def _render_property(prop: Property) -> str:
    return f"  {_render_field(prop.name, prop.type, prop.presence)}"


def _render_edge_property(prop: EdgeProperty) -> str:
    return _render_field(prop.name, prop.type, prop.presence)


def _render_edge(edge: Edge) -> str:
    properties = ", ".join(_render_edge_property(prop) for prop in edge.properties)
    body = f" {{{properties}}}" if properties else ""
    return f"(:{edge.source})-[:{edge.type}{body}]->(:{edge.target})  {edge.cardinality}"


def _render_node(node: Node) -> list[str]:
    return [node.label, *(_render_property(prop) for prop in node.properties)]


def render_slim_schema(schema: Schema) -> str:
    """Render `schema` as deterministic, newline-terminated plain text."""
    lines = ["NODES"]
    for node in schema.nodes:
        lines.extend(_render_node(node))
    if schema.edges:
        lines.append("EDGES")
        lines.extend(_render_edge(edge) for edge in schema.edges)
    return f"{_HEADER}\n" + "\n".join(lines) + "\n"
