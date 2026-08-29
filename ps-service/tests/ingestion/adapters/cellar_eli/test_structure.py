"""Tests for ps_service.ingestion.adapters.cellar_eli.structure.

Fixture XHTML is small, hand-trimmed, but structurally realistic against
real Cellar/ELI markup (`eli-container`/`eli-subdivision`/`eli-title`
classes, `cpt_*`/`art_*`/`rct_*`/`anx_*` id convention, `NNN.NNN`
paragraph ids) — see `spikes/cellar1/LEARNINGS.md` for the real structure
these mirror.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from ps_service.ingestion.adapters.cellar_eli.structure import (
    ANNEX,
    ARTICLE,
    CHAPTER,
    PARAGRAPH,
    RECITAL,
    _normalise_paragraph_number,  # pyright: ignore[reportPrivateUsage] — internal helper under test
    parse_structure,
)

if TYPE_CHECKING:
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
<div class="eli-subdivision" id="rct_1">Whereas this Regulation is necessary
for the internal market.</div>
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
    assert element_types == sorted(
        [CHAPTER, ARTICLE, ARTICLE, PARAGRAPH, PARAGRAPH, RECITAL, ANNEX]
    )


def test_title_shaped_div_is_not_minted_as_a_node() -> None:
    """S2: a TITLE-shaped div id (`ttl_1`, not matched by the structural
    id-dispatch table) is a transparent pass-through — it must never
    appear as a node's own element_type.
    """
    nodes, _ = _parse()

    assert not any(node.id.endswith("#ttl_1") for node in nodes)
    assert not any(node.element_type == "TITLE" for node in nodes)


def test_chapter_nested_under_title_pass_through_attaches_directly_to_regulation() -> None:
    """S2: since TITLE mints nothing, CHAPTER's parent edge must point
    straight at the Regulation — not at a TITLE node that doesn't exist.
    """
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

    assert (
        article_2.properties["text"]
        == "For the purposes of this Regulation, the following definitions apply."
    )
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

    assert (
        recital.properties["text"]
        == "Whereas this Regulation is necessary for the internal market."
    )
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


# --- Follow-on A2/A3: consolidated (amended) convention -------------------

_CONSOLIDATED_REGULATION_ID = "TEST-2.0"

# The consolidated (amendments-incorporated) Cellar convention, measured
# against real `02016R0679-20160504` / `02024R2847-20241120` markup
# (CONTEXT_FOLLOWON_A.md §0.2): one unnamed `eli-container` wrapping a
# single `enc_1` subdivision; article labels are `<p class="title-article-norm">`
# (NOT an `eli-title` div); paragraphs are `<div class="norm">` with a
# `<span class="no-parag">N. </span>` marker and no `id`; annexes are
# `anx_*` divs nested in the container (no separate container); no `rct_*`
# (the preamble is dropped from consolidated text).
_CONSOLIDATED_FIXTURE_XHTML = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container">
<div class="eli-subdivision" id="enc_1">
<div class="eli-subdivision" id="cpt_I">
<p class="title-division-2">CHAPTER I</p>
<div class="eli-subdivision" id="art_1">
<p class="title-article-norm">Article 1</p>
<div class="eli-title" id="art_1.tit_1">
<p class="stitle-article-norm">Subject matter</p></div>
<div class="norm"><span class="no-parag">1. </span>
<div class="norm inline-element">This Regulation lays down rules.</div></div>
<div class="norm"><span class="no-parag">2. </span>
<div class="norm inline-element">It lays down obligations too.</div></div>
</div>
<div class="eli-subdivision" id="art_2">
<p class="title-article-norm">Article 2</p>
<div class="eli-title" id="art_2.tit_1">
<p class="stitle-article-norm">Definitions</p></div>
<div class="norm">For the purposes of this Regulation, the following definitions apply.</div>
</div>
</div>
<div class="eli-subdivision" id="anx_I">
<p class="title-annex-1">ANNEX I</p>
<p class="norm">Technical requirements listed below.</p>
</div>
</div>
</div>
</body>
</html>
"""


def _parse_consolidated() -> tuple[tuple[StructuralNode, ...], tuple[StructuralEdge, ...]]:
    return parse_structure(_CONSOLIDATED_FIXTURE_XHTML, _CONSOLIDATED_REGULATION_ID)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1. ", "1"),
        ("001", "1"),
        ("0", "0"),
        ("26.", "26"),
        ("1a.", "1a"),
        ("\u20185a.", "5a"),
        ("  3b.  ", "3b"),
    ],
)
def test_normalise_paragraph_number(raw: str, expected: str) -> None:
    assert _normalise_paragraph_number(raw) == expected


def test_consolidated_paragraphs_minted_from_norm_no_parag_markup() -> None:
    nodes, _ = _parse_consolidated()
    paragraphs = sorted(
        (node for node in nodes if node.element_type == PARAGRAPH), key=lambda node: node.id
    )

    assert len(paragraphs) == 2
    assert paragraphs[0].properties["citation_ref"] == "Art. 1(1)"
    assert paragraphs[1].properties["citation_ref"] == "Art. 1(2)"
    assert "lays down rules" in str(paragraphs[0].properties["text"])


def test_consolidated_synthesised_paragraph_ids_are_unique_and_article_scoped() -> None:
    nodes, _ = _parse_consolidated()
    paragraph_ids = sorted(node.id for node in nodes if node.element_type == PARAGRAPH)

    assert paragraph_ids == [
        f"{_CONSOLIDATED_REGULATION_ID}#art_1.p1",
        f"{_CONSOLIDATED_REGULATION_ID}#art_1.p2",
    ]
    assert len(paragraph_ids) == len(set(paragraph_ids))


def test_consolidated_paragraph_edges_link_to_their_article() -> None:
    _, edges = _parse_consolidated()
    article_1_id = f"{_CONSOLIDATED_REGULATION_ID}#art_1"

    paragraph_edges = [
        edge
        for edge in edges
        if edge.parent_id == article_1_id and edge.child_element_type == PARAGRAPH
    ]
    assert len(paragraph_edges) == 2
    assert all(edge.parent_element_type == ARTICLE for edge in paragraph_edges)


def test_consolidated_article_label_does_not_leak_into_article_text() -> None:
    nodes, _ = _parse_consolidated()

    for article in (node for node in nodes if node.element_type == ARTICLE):
        assert not str(article.properties["text"]).startswith("Article ")


def test_consolidated_multi_paragraph_article_has_empty_own_text_and_a_heading() -> None:
    nodes, _ = _parse_consolidated()
    article_1 = next(node for node in nodes if node.id == f"{_CONSOLIDATED_REGULATION_ID}#art_1")

    assert article_1.properties["text"] == ""
    assert article_1.properties["heading"] == "Subject matter"
    assert article_1.properties["citation_ref"] == "Art. 1"


def test_consolidated_single_block_article_keeps_its_own_text() -> None:
    nodes, _ = _parse_consolidated()
    article_2 = next(node for node in nodes if node.id == f"{_CONSOLIDATED_REGULATION_ID}#art_2")

    assert (
        article_2.properties["text"]
        == "For the purposes of this Regulation, the following definitions apply."
    )


def test_consolidated_text_without_recitals_mints_none_and_does_not_crash() -> None:
    nodes, _ = _parse_consolidated()

    assert not any(node.element_type == RECITAL for node in nodes)


def test_consolidated_chapter_is_still_minted_even_without_an_eli_title_heading() -> None:
    nodes, _ = _parse_consolidated()
    chapter = _node(nodes, CHAPTER)

    assert chapter.id == f"{_CONSOLIDATED_REGULATION_ID}#cpt_I"
    assert chapter.properties["citation_ref"] == "Chapter I"


# --- Follow-on A3: unified ANNEX minting ---------------------------------


def test_consolidated_annex_minted_from_div_nested_in_the_main_container() -> None:
    nodes, edges = _parse_consolidated()
    annex = _node(nodes, ANNEX)

    assert annex.id == f"{_CONSOLIDATED_REGULATION_ID}#anx_I"
    assert annex.properties["citation_ref"] == "Annex I"
    assert "Technical requirements listed below." in str(annex.properties["text"])

    annex_edges = [edge for edge in edges if edge.child_id == annex.id]
    assert len(annex_edges) == 1
    assert annex_edges[0].parent_element_type == "RegulatoryInstrument"
    assert annex_edges[0].parent_id == _CONSOLIDATED_REGULATION_ID


_BOTH_ANNEX_FORMS_FIXTURE = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container">
<div class="eli-subdivision" id="enc_1">
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Scope</div>
<div id="001.001">1. Rules.</div>
</div>
<div class="eli-subdivision" id="anx_I">
<p class="title-annex-1">ANNEX I</p>
<p class="norm">Nested annex body.</p>
</div>
</div>
</div>
<div class="eli-container" id="anx_I">
<div class="eli-title" id="anx_I.tit_1">ANNEX I Separate container body</div>
</div>
</body>
</html>
"""


def test_annex_present_in_both_markup_forms_is_minted_exactly_once() -> None:
    nodes, edges = parse_structure(_BOTH_ANNEX_FORMS_FIXTURE, "BOTH-1.0")

    annexes = [node for node in nodes if node.element_type == ANNEX]
    assert len(annexes) == 1
    assert annexes[0].properties["citation_ref"] == "Annex I"

    annex_edges = [edge for edge in edges if edge.child_element_type == ANNEX]
    assert len(annex_edges) == 1
