"""Tests for ps_service.ingestion.adapters.cellar_eli.structure.

Fixture XHTML is small, hand-trimmed, but structurally realistic against
real Cellar/ELI markup (`eli-container`/`eli-subdivision`/`eli-title`
classes, `cpt_*`/`art_*`/`rct_*`/`anx_*` id convention, `NNN.NNN`
paragraph ids) — see `spikes/cellar1/LEARNINGS.md` for the real structure
these mirror.
"""

from __future__ import annotations

from ps_service.ingestion.adapters.cellar_eli.structure import (
    ANNEX,
    ARTICLE,
    CHAPTER,
    PARAGRAPH,
    RECITAL,
    parse_structure,
)
from ps_service.ingestion.models import StructuralEdge, StructuralNode

_REGULATION_ID = "TEST-1.0"

# One CHAPTER (wrapped in a TITLE-shaped pass-through div, proving TITLE
# mints nothing and its children attach to the current parent) containing
# one multi-paragraph ARTICLE and one single-block ARTICLE; one top-level
# RECITAL (sibling of the TITLE div, not nested inside it); one top-level
# ANNEX in its own separate eli-container.
_FIXTURE_XHTML = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div id="ttl_1">
<div class="eli-subdivision" id="cpt_I">
<div class="eli-title" id="cpt_I.tit_1">CHAPTER I General provisions</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Subject matter</div>
<div id="001.001">1. This Regulation lays down rules.</div>
<div id="001.002">2. It also lays down obligations.</div>
</div>
<div class="eli-subdivision" id="art_2">
<div class="eli-title" id="art_2.tit_1">Article 2 Definitions</div>
<div>For the purposes of this Regulation, the following definitions apply.</div>
</div>
</div>
</div>
<div class="eli-subdivision" id="rct_1">Whereas this Regulation is necessary for the internal market.</div>
</div>
<div class="eli-container" id="anx_I">
<div class="eli-title" id="anx_I.tit_1">ANNEX I Technical requirements</div>
<div>Requirements listed below.</div>
</div>
</body>
</html>
"""


def _parse() -> tuple[tuple[StructuralNode, ...], tuple[StructuralEdge, ...]]:
    return parse_structure(_FIXTURE_XHTML, _REGULATION_ID)


def _node(nodes: tuple[StructuralNode, ...], element_type: str) -> StructuralNode:
    matches = [node for node in nodes if node.element_type == element_type]
    assert len(matches) == 1, f"expected exactly one {element_type} node, found {len(matches)}"
    return matches[0]


def test_parse_structure_mints_exactly_the_expected_node_count() -> None:
    nodes, _ = _parse()

    assert len(nodes) == 7  # CHAPTER, 2x ARTICLE, 2x PARAGRAPH, RECITAL, ANNEX


def test_parse_structure_mints_exactly_the_expected_edge_count() -> None:
    _, edges = _parse()

    assert len(edges) == 7  # one parent edge per minted node (tree shape)


def test_parse_structure_mints_expected_element_types() -> None:
    nodes, _ = _parse()

    element_types = sorted(node.element_type for node in nodes)
    assert element_types == sorted([CHAPTER, ARTICLE, ARTICLE, PARAGRAPH, PARAGRAPH, RECITAL, ANNEX])


def test_title_shaped_div_is_not_minted_as_a_node() -> None:
    """S2: a TITLE-shaped div id (`ttl_1`, not matched by the structural
    id-dispatch table) is a transparent pass-through — it must never
    appear as a node's own element_type."""
    nodes, _ = _parse()

    assert not any(node.id.endswith("#ttl_1") for node in nodes)
    assert not any(node.element_type == "TITLE" for node in nodes)


def test_chapter_nested_under_title_pass_through_attaches_directly_to_regulation() -> None:
    """S2: since TITLE mints nothing, CHAPTER's parent edge must point
    straight at the Regulation — not at a TITLE node that doesn't exist."""
    nodes, edges = _parse()
    chapter = _node(nodes, CHAPTER)

    chapter_edges = [edge for edge in edges if edge.child_id == chapter.id]
    assert len(chapter_edges) == 1
    assert chapter_edges[0].parent_element_type == "RegulatoryInstrument"
    assert chapter_edges[0].parent_id == _REGULATION_ID


def test_chapter_heading_extracted_from_eli_title_child() -> None:
    nodes, _ = _parse()
    chapter = _node(nodes, CHAPTER)

    assert chapter.properties["heading"] == "CHAPTER I General provisions"
    assert chapter.properties["citation_ref"] == "Chapter I"


def test_multi_paragraph_article_has_no_own_text_and_two_paragraph_children() -> None:
    nodes, edges = _parse()
    article_1 = next(node for node in nodes if node.id == f"{_REGULATION_ID}#art_1")

    assert article_1.properties["text"] == ""
    assert article_1.properties["heading"] == "Article 1 Subject matter"
    assert article_1.properties["citation_ref"] == "Art. 1"

    paragraph_edges = [edge for edge in edges if edge.parent_id == article_1.id]
    assert len(paragraph_edges) == 2
    assert all(edge.child_element_type == PARAGRAPH for edge in paragraph_edges)


def test_paragraph_citation_refs_and_text_are_extracted_per_paragraph() -> None:
    nodes, _ = _parse()
    paragraph_1 = next(node for node in nodes if node.id == f"{_REGULATION_ID}#001.001")
    paragraph_2 = next(node for node in nodes if node.id == f"{_REGULATION_ID}#001.002")

    assert paragraph_1.properties["citation_ref"] == "Art. 1(1)"
    assert paragraph_1.properties["text"] == "1. This Regulation lays down rules."
    assert paragraph_2.properties["citation_ref"] == "Art. 1(2)"
    assert paragraph_2.properties["text"] == "2. It also lays down obligations."


def test_single_block_article_has_own_text_and_no_paragraph_children() -> None:
    nodes, edges = _parse()
    article_2 = next(node for node in nodes if node.id == f"{_REGULATION_ID}#art_2")

    assert article_2.properties["text"] == "For the purposes of this Regulation, the following definitions apply."
    assert article_2.properties["citation_ref"] == "Art. 2"

    child_edges = [edge for edge in edges if edge.parent_id == article_2.id]
    assert child_edges == []


def test_both_articles_are_children_of_the_chapter() -> None:
    nodes, edges = _parse()
    chapter = _node(nodes, CHAPTER)
    article_ids = {node.id for node in nodes if node.element_type == ARTICLE}

    article_edges = [edge for edge in edges if edge.child_id in article_ids]
    assert len(article_edges) == 2
    assert all(edge.parent_id == chapter.id for edge in article_edges)
    assert all(edge.parent_element_type == CHAPTER for edge in article_edges)


def test_recital_is_top_level_child_of_regulation_with_its_own_text() -> None:
    nodes, edges = _parse()
    recital = _node(nodes, RECITAL)

    assert recital.properties["text"] == "Whereas this Regulation is necessary for the internal market."
    assert recital.properties["citation_ref"] == "Recital 1"

    recital_edges = [edge for edge in edges if edge.child_id == recital.id]
    assert len(recital_edges) == 1
    assert recital_edges[0].parent_element_type == "RegulatoryInstrument"
    assert recital_edges[0].parent_id == _REGULATION_ID


def test_annex_is_top_level_child_of_regulation_sourced_from_its_own_container() -> None:
    nodes, edges = _parse()
    annex = _node(nodes, ANNEX)

    assert annex.id == f"{_REGULATION_ID}#anx_I"
    assert annex.properties["citation_ref"] == "Annex I"
    assert annex.properties["text"] == "ANNEX I Technical requirements Requirements listed below."

    annex_edges = [edge for edge in edges if edge.child_id == annex.id]
    assert len(annex_edges) == 1
    assert annex_edges[0].parent_element_type == "RegulatoryInstrument"
    assert annex_edges[0].parent_id == _REGULATION_ID
