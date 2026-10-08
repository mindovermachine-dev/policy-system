"""The slim output contract on the real `DOMAIN_SCHEMA` (AC-BI-009, 011, 012 proxy, 019).

To regenerate the golden after a deliberate schema or format change:
write `render_slim_schema(DOMAIN_SCHEMA)` (no trailing newline added) to
`golden/slim.txt` next to this file and review the diff by eye.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from ps_service.domain_schema import DOMAIN_SCHEMA, Edge, Node, Presence, render_slim_schema

_GOLDEN = Path(__file__).resolve().parent / "golden" / "slim.txt"
_LEGEND = (
    'Capability status "merged" is a tombstone (see MERGED_INTO to its survivor); '
    'readers filter status = "active" unless they want tombstones.'
)
_MEASURED_SLIM_CHARS = 3512  # `python -m ps_service.domain_schema measure`, after=
_CEILING_FACTOR = 1.25
_PRESENCE_MARK = {Presence.REQUIRED: " !", Presence.CONDITIONAL: " ?", Presence.OPTIONAL: ""}


def _packaged_doc() -> str:
    return (
        files("ps_service.mcp_interface")
        .joinpath("ps-domain-concepts.md")
        .read_text(encoding="utf-8")
    )


def _node_section(text: str, node: Node) -> list[str]:
    lines = text.splitlines()
    start = lines.index(node.label)
    section: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        section.append(line)
    return section


def _edge_line_prefix(edge: Edge) -> str:
    return f"(:{edge.source})-[:{edge.type}"


def test_every_label_property_and_edge_is_present() -> None:
    """AC-BI-009: names, type text, enum values, required marks, edge ends and cardinality."""
    text = render_slim_schema(DOMAIN_SCHEMA)
    lines = text.splitlines()

    for node in DOMAIN_SCHEMA.nodes:
        section = _node_section(text, node)
        assert len(section) == len(node.properties)
        for prop, line in zip(node.properties, section, strict=True):
            assert line.startswith(f"  {prop.name}: ")
            assert line.endswith(_PRESENCE_MARK[prop.presence])
    for edge in DOMAIN_SCHEMA.edges:
        [line] = [
            candidate
            for candidate in lines
            if candidate.startswith(_edge_line_prefix(edge))
            and candidate.split("->")[1].startswith(f"(:{edge.target})")
        ]
        assert line.endswith(f"  {edge.cardinality}")
        for prop in edge.properties:
            assert f"{prop.name}: " in line


def test_slim_output_has_no_notes_and_no_prose() -> None:
    """AC-BI-009: no note / rule text from the schema and no document sections."""
    text = render_slim_schema(DOMAIN_SCHEMA)

    notes = [prop.note for node in DOMAIN_SCHEMA.nodes for prop in node.properties if prop.note]
    notes += [node.note for node in DOMAIN_SCHEMA.nodes if node.note]
    notes += [edge.note for edge in DOMAIN_SCHEMA.edges if edge.note]
    notes += [edge.provenance_rule for edge in DOMAIN_SCHEMA.edges if edge.provenance_rule]
    assert notes
    for note in notes:
        assert note not in text
    assert "##" not in text
    assert "Lifecycle" not in text
    assert "Identity" not in text


def test_slim_is_strictly_shorter_than_full_document() -> None:
    """AC-BI-011: the tool result is smaller than the document it replaces."""
    assert len(render_slim_schema(DOMAIN_SCHEMA)) < len(_packaged_doc())


def test_slim_output_is_deterministic() -> None:
    """AC-BI-009: rendering twice yields identical text."""
    assert render_slim_schema(DOMAIN_SCHEMA) == render_slim_schema(DOMAIN_SCHEMA)


def test_slim_output_matches_golden() -> None:
    """AC-BI-009: the whole output is pinned; see the module docstring to regenerate."""
    assert render_slim_schema(DOMAIN_SCHEMA) == _GOLDEN.read_text(encoding="utf-8")


def test_legend_states_tombstone_and_active_default() -> None:
    """F7: the legend explains `merged` tombstones and the `status = "active"` default."""
    lines = render_slim_schema(DOMAIN_SCHEMA).splitlines()

    assert _LEGEND in lines
    assert len(_LEGEND) <= 200  # legend length cap from CHANGES A7
    assert lines.index(_LEGEND) == 3  # fourth header line, after cardinality legend
    assert lines[2].startswith('Cardinality "S : T"')


def test_slim_size_stays_within_the_recorded_ceiling() -> None:
    """AC-BI-012 proxy: the result stays near the measured size so it stays inline."""
    assert len(render_slim_schema(DOMAIN_SCHEMA)) <= _CEILING_FACTOR * _MEASURED_SLIM_CHARS
