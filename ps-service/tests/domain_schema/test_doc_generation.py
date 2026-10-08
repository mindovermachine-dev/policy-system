"""The committed domain-concepts document's generated regions equal the schema's rendering."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ps_service.domain_schema import DOMAIN_SCHEMA
from ps_service.domain_schema.render_doc import (
    begin_marker,
    end_marker,
    find_region_ids,
    relationships_region_id,
    render_edge_catalog,
    render_properties_table,
    render_relationships_table,
    section_anchor,
)

_REPO = Path(__file__).resolve().parents[3]
_DOC = _REPO / "docs" / "artifacts" / "ps-domain-concepts.md"
_CLASSIFICATION_EDGES = ("COVERS", "OWNS", "MITIGATED_BY", "VERIFIED_BY")


def doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


def region_body(text: str, region_id: str) -> str:
    """Return the table between the markers of `region_id` (without the blank padding lines)."""
    after_begin = text.split(begin_marker(region_id) + "\n", 1)[1]
    return after_begin.split(end_marker(region_id), 1)[0].strip("\n") + "\n"


def test_edge_catalog_region_is_fresh() -> None:
    """AC-BI-007: the doc's edge-catalog region equals the rendered catalog."""
    assert region_body(doc_text(), "edge-catalog") == render_edge_catalog(DOMAIN_SCHEMA)


def test_every_marker_says_generated_and_do_not_edit() -> None:
    """AC-BI-018: each BEGIN marker names the generator and says not to edit by hand."""
    text = doc_text()
    region_ids = find_region_ids(text)

    assert region_ids
    for region_id in region_ids:
        assert begin_marker(region_id) in text
        assert end_marker(region_id) in text
        assert "Generated from ps_service.domain_schema" in begin_marker(region_id)
        assert "do not edit by hand" in begin_marker(region_id)


def test_edge_catalog_has_one_row_per_schema_edge_and_the_arrow_header() -> None:
    """F6: header uses U+2192; every one of the 16 schema edges has a row."""
    lines = region_body(doc_text(), "edge-catalog").splitlines()

    assert lines[0].startswith("| Edge | Source → Target | Cardinality |")
    assert len(lines) - 2 == len(DOMAIN_SCHEMA.edges)


def test_classification_edges_show_the_many_to_many_cardinality() -> None:
    """D-F1: COVERS, OWNS, MITIGATED_BY, VERIFIED_BY read many-to-many in the catalog."""
    lines = region_body(doc_text(), "edge-catalog").splitlines()

    for edge_type in _CLASSIFICATION_EDGES:
        [row] = [line for line in lines if line.startswith(f"| `{edge_type}` |")]
        assert " | 1..\\* : 0..* | " in row


@pytest.mark.parametrize("label", [node.label for node in DOMAIN_SCHEMA.nodes])
def test_properties_region_is_fresh(label: str) -> None:
    """AC-BI-007: each node's properties region equals the rendered table."""
    node = next(n for n in DOMAIN_SCHEMA.nodes if n.label == label)

    assert region_body(doc_text(), f"properties:{label}") == render_properties_table(node)


def test_policy_lifecycle_prose_names_the_proposed_step() -> None:
    """D4: the Policy status prose lists draft -> proposed -> approved -> deprecated."""
    assert "`draft` → `proposed` → `approved` → `deprecated`" in doc_text()


@pytest.mark.parametrize("label", [node.label for node in DOMAIN_SCHEMA.nodes])
def test_relationships_region_is_fresh(label: str) -> None:
    """AC-BI-007: each node's relationships region equals the rendered table."""
    body = region_body(doc_text(), relationships_region_id(label))

    assert body == render_relationships_table(DOMAIN_SCHEMA, label)


def test_inbound_cardinality_is_flip_of_catalog() -> None:
    """D7: every inbound row shows the flip of the catalog cardinality of its edge."""
    text = doc_text()
    for edge in DOMAIN_SCHEMA.edges:
        rows = region_body(text, relationships_region_id(edge.target)).splitlines()
        [row] = [r for r in rows if r.startswith(f"| `{edge.type}` (inbound) | {edge.source} |")]
        cardinality = row.split(" | ")[2].replace("\\*", "*")
        assert cardinality == str(edge.cardinality.flipped())


def test_section_anchors_exist_in_doc() -> None:
    """D7: each `#anchor` linked from an inbound note is a heading in the document."""
    text = doc_text()
    headings = {
        re.sub(r"[^a-z0-9 -]", "", line.removeprefix("### ").lower()).replace(" ", "-")
        for line in text.splitlines()
        if line.startswith("### ")
    }

    for node in DOMAIN_SCHEMA.nodes:
        assert section_anchor(node.label) in headings
