"""Render schema-derived markdown tables and splice them into the domain-concepts document.

Generated tables live between `BEGIN GENERATED <id>` and `END GENERATED <id>` marker lines;
`replace_regions` rewrites only the bytes between a marker pair and returns everything
outside the markers unchanged.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ps_service.domain_schema.errors import DomainSchemaError
from ps_service.domain_schema.model import (
    ConstType,
    DateType,
    EnumType,
    FloatRangeType,
    Presence,
    StringType,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.domain_schema.model import (
        Edge,
        EdgeProperty,
        Node,
        Property,
        PropertyType,
        Schema,
    )

EDGE_CATALOG_REGION = "edge-catalog"
_REGENERATE_COMMAND = "uv run python -m ps_service.domain_schema write-docs"
_BEGIN_PREFIX = "<!-- BEGIN GENERATED "
_END_PREFIX = "<!-- END GENERATED "
_MARKER_LINE = re.compile(
    r"<!-- (?P<kind>BEGIN|END) GENERATED (?P<id>[A-Za-z][A-Za-z0-9:-]*)(?:\. .*)? -->\Z"
)
_ARROW = "→"
_EM_DASH = "—"
_EN_DASH = "\u2013"
_PRESENCE_TEXT = {
    Presence.REQUIRED: "Yes",
    Presence.OPTIONAL: "No",
    Presence.CONDITIONAL: "Conditional",
}
_EDGE_PRESENCE_TEXT = {
    Presence.REQUIRED: "required",
    Presence.OPTIONAL: "optional",
    Presence.CONDITIONAL: "conditional",
}


def begin_marker(region_id: str) -> str:
    """Return the BEGIN marker line (without newline) for `region_id`.

    Args:
        region_id: The generated region's id, for example `edge-catalog`; must match the marker
            id pattern (a letter, then letters, digits, `:` or `-`).

    Returns:
        An HTML comment line that names the region and the command that regenerates it.
    """
    return (
        f"{_BEGIN_PREFIX}{region_id}. Generated from ps_service.domain_schema, "
        f"do not edit by hand. Regenerate with: {_REGENERATE_COMMAND} -->"
    )


def end_marker(region_id: str) -> str:
    """Return the END marker line (without newline) for `region_id`.

    Args:
        region_id: The id of the region the line closes.

    Returns:
        An HTML comment line that closes the region.
    """
    return f"{_END_PREFIX}{region_id} -->"


def _line_body(line: str) -> str:
    """Return `line` without its trailing line break."""
    return line.rstrip("\r\n")


def _marker_of(line: str) -> tuple[str, str] | None:
    """Return (`BEGIN`|`END`, region id) when `line` is a marker line."""
    matched = _MARKER_LINE.match(_line_body(line))
    if matched is None:
        return None
    return matched.group("kind"), matched.group("id")


def find_region_ids(text: str) -> tuple[str, ...]:
    """Return the region ids of every BEGIN marker in `text`, in document order.

    Args:
        text: A document that may contain generated-region markers.

    Returns:
        The ids in order of appearance, duplicates included; empty when there are no markers.
    """
    ids: list[str] = []
    for line in text.splitlines():
        marker = _marker_of(line)
        if marker is not None and marker[0] == "BEGIN":
            ids.append(marker[1])
    return tuple(ids)


def replace_regions(text: str, rendered: Mapping[str, str]) -> str:
    """Return `text` with each marked region's body replaced by `rendered[region id]`.

    The marker lines and every byte outside them are kept as found; a region body becomes a
    blank line, the rendered table (LF line endings) and a blank line.

    Args:
        text: The document containing BEGIN/END marker pairs.
        rendered: Region id -> rendered table text; must hold an entry for every region in `text`.

    Returns:
        The updated document text.

    Raises:
        DomainSchemaError: on a BEGIN without END, an END without BEGIN or for another id, a
            nested or duplicate region, or a region id with no rendered content.
    """
    output: list[str] = []
    open_id: str | None = None
    seen: set[str] = set()
    for line in text.splitlines(keepends=True):
        marker = _marker_of(line)
        if marker is None:
            if open_id is None:
                output.append(line)
            continue
        kind, region_id = marker
        if kind == "BEGIN":
            _check_can_open(open_id, region_id, seen, rendered)
            seen.add(region_id)
            open_id = region_id
            output.extend((line, "\n", rendered[region_id], "\n"))
        else:
            if open_id != region_id:
                raise DomainSchemaError(f"END marker for region {region_id!r} without its BEGIN")
            open_id = None
            output.append(line)
    if open_id is not None:
        raise DomainSchemaError(f"region {open_id!r} has no END marker")
    return "".join(output)


def _check_can_open(
    open_id: str | None, region_id: str, seen: set[str], rendered: Mapping[str, str]
) -> None:
    """Raise unless region `region_id` may open: not nested, not repeated, and renderable."""
    if open_id is not None:
        raise DomainSchemaError(f"region {region_id!r} is nested inside region {open_id!r}")
    if region_id in seen:
        raise DomainSchemaError(f"duplicate region {region_id!r}")
    if region_id not in rendered:
        raise DomainSchemaError(f"unknown region {region_id!r}: the schema renders no content")


def _cell(text: str) -> str:
    """Escape pipes so `text` stays inside one markdown table cell."""
    return text.replace("|", "\\|")


def _escape_cardinality(cardinality: str) -> str:
    """Escape the first `*` when both sides contain one, so markdown does not read emphasis."""
    left, right = cardinality.split(" : ")
    if "*" in left and "*" in right:
        return f"{left.replace('*', chr(92) + '*', 1)} : {right}"
    return cardinality


def _type_text(property_type: PropertyType) -> str:
    """Return the markdown text for a property type, for example `date (ISO 8601)` or `string`."""
    match property_type:
        case StringType():
            return "string"
        case DateType():
            return "date (ISO 8601)"
        case FloatRangeType(minimum=low, maximum=high):
            return f"float, {low}{_EN_DASH}{high}"
        case EnumType(values=values):
            return "enum: " + " \\| ".join(f"`{value}`" for value in values)
        case ConstType(value=value):
            return f"const: `{value}`"


def _edge_properties_catalog(properties: tuple[EdgeProperty, ...]) -> str:
    """Return an edge's properties for the Edge Catalog cell, or an em dash when it has none."""
    if not properties:
        return _EM_DASH
    return ", ".join(f"`{p.name}` ({_EDGE_PRESENCE_TEXT[p.presence]})" for p in properties)


def _catalog_row(edge: Edge) -> str:
    """Return one Edge Catalog table row for `edge`."""
    cells = (
        f"`{edge.type}`",
        f"{edge.source} {_ARROW} {edge.target}",
        _escape_cardinality(str(edge.cardinality)),
        _edge_properties_catalog(edge.properties),
        _cell(edge.provenance_rule),
    )
    return "| " + " | ".join(cells) + " |"


def render_edge_catalog(schema: Schema) -> str:
    """Render the Edge Catalog table: one row per schema edge, in schema order.

    Args:
        schema: The schema whose edges are listed.

    Returns:
        A markdown table (header, separator, rows), newline-terminated.
    """
    header = (
        (
            "| Edge | Source → Target | Cardinality | Properties "
            "| Rule case ([above](#provenance-placement-rule)) |"
        ),
        "|------|------------------|-------------|------------|------|",
    )
    rows = (_catalog_row(edge) for edge in schema.edges)
    return "\n".join((*header, *rows)) + "\n"


def _property_row(prop: Property) -> str:
    """Return one Properties table row for `prop`."""
    prefix = f"| `{prop.name}` | {_type_text(prop.type)} | {_PRESENCE_TEXT[prop.presence]} |"
    note = f" {_cell(prop.note)}" if prop.note else ""
    return f"{prefix}{note} |"


def render_properties_table(node: Node) -> str:
    """Render a node's Properties table: one row per property, in declaration order.

    Args:
        node: The node whose properties are listed.

    Returns:
        A markdown table (header, separator, rows), newline-terminated.
    """
    header = ("| Property | Type | Required | Notes |", "|----------|------|----------|-------|")
    return "\n".join((*header, *(_property_row(prop) for prop in node.properties))) + "\n"


def properties_region_id(label: str) -> str:
    """Return the region id of the Properties table of node `label`.

    Args:
        label: A node label.

    Returns:
        The id `properties:<label>`.
    """
    return f"properties:{label}"


# Headings whose GitHub anchor is not the lower-cased label (the RI heading is a phrase).
_ANCHOR_BY_LABEL = {"RegulatoryInstrument": "regulatory-instrument"}


def section_anchor(label: str) -> str:
    """Return the in-document anchor of the heading that describes node `label`.

    Args:
        label: A node label.

    Returns:
        The heading anchor: the lower-cased label, except for the labels in a small override map
        whose heading is a phrase (`RegulatoryInstrument` -> `regulatory-instrument`).
    """
    return _ANCHOR_BY_LABEL.get(label, label.lower())


def _edge_properties_relationship(properties: tuple[EdgeProperty, ...]) -> str:
    """Return an edge's properties with types for a Relationships cell, or an em dash if none."""
    if not properties:
        return _EM_DASH
    return ", ".join(
        f"`{p.name}` ({_type_text(p.type)}, {_EDGE_PRESENCE_TEXT[p.presence]})" for p in properties
    )


def _relationship_row(edge: Edge, *, inbound: bool) -> str:
    """Return one Relationships row: the edge seen from its target (inbound) or source."""
    other = edge.source if inbound else edge.target
    cardinality = edge.cardinality.flipped() if inbound else edge.cardinality
    direction = "inbound" if inbound else "outbound"
    note = (
        f"See [{edge.source} {_ARROW} {edge.type}](#{section_anchor(edge.source)})."
        if inbound
        else edge.note
    )
    cells = (
        f"`{edge.type}` ({direction})",
        other,
        _escape_cardinality(str(cardinality)),
        _edge_properties_relationship(edge.properties),
        _cell(note),
    )
    return "| " + " | ".join(cells).rstrip() + " |"


def render_relationships_table(schema: Schema, label: str) -> str:
    """Render the Relationships table of `label`: inbound edges first, then outbound.

    Args:
        schema: The schema whose edges are scanned.
        label: The node label whose relationships are listed.

    Returns:
        A markdown table (header, separator, rows), newline-terminated; header only when the
        label has no edges.
    """
    header = (
        "| Edge | Target | Cardinality | Edge Properties | Note |",
        "|------|--------|-------------|------------------|------|",
    )
    inbound = (_relationship_row(e, inbound=True) for e in schema.edges if e.target == label)
    outbound = (_relationship_row(e, inbound=False) for e in schema.edges if e.source == label)
    return "\n".join((*header, *inbound, *outbound)) + "\n"


def relationships_region_id(label: str) -> str:
    """Return the region id of the Relationships table of node `label`.

    Args:
        label: A node label.

    Returns:
        The id `relationships:<label>`.
    """
    return f"relationships:{label}"


def render_regions(schema: Schema) -> dict[str, str]:
    """Return every generated region id with its rendered table.

    Args:
        schema: The schema to render.

    Returns:
        A mapping holding the `edge-catalog` region, then a Properties and a Relationships region
        per node, in schema order.
    """
    regions = {EDGE_CATALOG_REGION: render_edge_catalog(schema)}
    for node in schema.nodes:
        regions[properties_region_id(node.label)] = render_properties_table(node)
        regions[relationships_region_id(node.label)] = render_relationships_table(
            schema, node.label
        )
    return regions
