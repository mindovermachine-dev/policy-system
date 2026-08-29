"""Tests for `ps_service.domain_mapper.adapters.cellar_eli`.

Fixture rows mirror the exact shape #14's `ps_service.ingestion.adapters.
cellar_eli.structure` writes (cross-checked directly against that module
and against `tests/ingestion/adapters/cellar_eli/test_structure.py`'s own
fixtures) — `citation_ref` values like `"Art. 1"`/`"Art. 1(1)"`, node ids
like `"reg#art_1"`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ps_service.domain_mapper.adapters.cellar_eli import CellarEliDomainMappingAdapter
from ps_service.domain_mapper.models import ExtractionUnit

if TYPE_CHECKING:
    from ps_service.domain_mapper.falkordb_client import GraphQueryResult


@dataclass(frozen=True)
class _FakeNode:
    """One native-graph node, labeled the same way #14's `structure.py`
    labels it (`ARTICLE`, `PARAGRAPH`, `RECITAL`, `ANNEX`, `CHAPTER`,
    `SECTION`).
    """

    label: str
    id: str
    citation_ref: str
    text: str = ""
    heading: str = ""


class _FakeGraphQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


@dataclass
class _FakeGraphHandle:
    """Hand-written structural fake for `GraphHandle`. Holds every node —
    including `RECITAL`/`ANNEX`/`CHAPTER`/`SECTION`, to prove the adapter's
    scope decision structurally — and filters by label the same way a real
    `MATCH (a:ARTICLE)` Cypher query would: a non-`ARTICLE`-labeled node can
    never surface via that query, regardless of what else is present in the
    same graph.
    """

    nodes: list[_FakeNode]
    article_children: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._nodes_by_id = {node.id: node for node in self.nodes}

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        if q.strip().startswith("MATCH (a:ARTICLE)"):
            rows = [
                [node.id, node.citation_ref, node.text, node.heading]
                for node in self.nodes
                if node.label == "ARTICLE"
            ]
            rows.sort(key=lambda row: row[0])
            return _FakeGraphQueryResult([*rows])
        if "PARAGRAPH" in q:
            assert params is not None
            article_id = params["id"]
            assert isinstance(article_id, str)
            child_ids = self.article_children.get(article_id, [])
            rows = [
                [self._nodes_by_id[child_id].citation_ref, self._nodes_by_id[child_id].text]
                for child_id in child_ids
            ]
            return _FakeGraphQueryResult([*rows])
        raise AssertionError(f"unexpected query: {q!r}")


def _adapter() -> CellarEliDomainMappingAdapter:
    return CellarEliDomainMappingAdapter()


def test_reads_one_unit_per_paragraph_when_article_has_them() -> None:
    graph = _FakeGraphHandle(
        nodes=[
            _FakeNode("ARTICLE", "reg#art_1", "Art. 1", heading="Subject matter"),
            _FakeNode(
                "PARAGRAPH", "reg#001.001", "Art. 1(1)", text="This Regulation lays down rules."
            ),
            _FakeNode(
                "PARAGRAPH", "reg#001.002", "Art. 1(2)", text="It also lays down obligations."
            ),
        ],
        article_children={"reg#art_1": ["reg#001.001", "reg#001.002"]},
    )

    units = _adapter().read_native_units(graph)

    assert units == (
        ExtractionUnit(
            citation_ref="Art. 1(1)",
            text="This Regulation lays down rules.",
            article_number="1",
            paragraph_number="1",
            article_heading="Subject matter",
        ),
        ExtractionUnit(
            citation_ref="Art. 1(2)",
            text="It also lays down obligations.",
            article_number="1",
            paragraph_number="2",
            article_heading="Subject matter",
        ),
    )


def test_reads_one_unit_for_whole_article_when_it_has_no_paragraphs() -> None:
    """Paragraph-less-Article fallback: `paragraph_number == "1"`."""
    graph = _FakeGraphHandle(
        nodes=[
            _FakeNode(
                "ARTICLE",
                "reg#art_2",
                "Art. 2",
                text="Definitions apply.",
                heading="Definitions",
            ),
        ],
        article_children={},
    )

    units = _adapter().read_native_units(graph)

    assert units == (
        ExtractionUnit(
            citation_ref="Art. 2",
            text="Definitions apply.",
            article_number="2",
            paragraph_number="1",
            article_heading="Definitions",
        ),
    )


def test_document_order_is_by_numeric_article_and_paragraph_number_not_id_string() -> None:
    """`ORDER BY a.id` sorts `"art_10"` before `"art_2"` lexicographically —
    the adapter must still return units in true document order (Art. 1
    before Art. 2 before Art. 10).
    """
    graph = _FakeGraphHandle(
        nodes=[
            _FakeNode("ARTICLE", "reg#art_1", "Art. 1", heading="Article 1"),
            _FakeNode("PARAGRAPH", "reg#001.001", "Art. 1(1)", text="Para 1.1"),
            _FakeNode("PARAGRAPH", "reg#001.002", "Art. 1(2)", text="Para 1.2"),
            _FakeNode(
                "ARTICLE", "reg#art_10", "Art. 10", text="Article 10 text", heading="Article 10"
            ),
            _FakeNode("ARTICLE", "reg#art_2", "Art. 2", text="Article 2 text", heading="Article 2"),
        ],
        article_children={"reg#art_1": ["reg#001.001", "reg#001.002"]},
    )

    units = _adapter().read_native_units(graph)

    assert [unit.citation_ref for unit in units] == [
        "Art. 1(1)",
        "Art. 1(2)",
        "Art. 2",
        "Art. 10",
    ]


def test_recital_annex_chapter_section_nodes_are_ignored() -> None:
    """Open Question 4, locked in: only ARTICLE/PARAGRAPH ever become
    ExtractionUnits — RECITAL/ANNEX/CHAPTER/SECTION nodes present in the
    same native graph must never surface, structurally proven via the
    fake's label-filtered `MATCH (a:ARTICLE)` query.
    """
    graph = _FakeGraphHandle(
        nodes=[
            _FakeNode("ARTICLE", "reg#art_1", "Art. 1", text="Article 1 text", heading="Article 1"),
            _FakeNode("RECITAL", "reg#rct_1", "Recital 1", text="Whereas this is necessary."),
            _FakeNode("ANNEX", "reg#anx_I", "Annex I", text="Technical requirements."),
            _FakeNode("CHAPTER", "reg#cpt_I", "Chapter I", heading="General provisions"),
            _FakeNode("SECTION", "reg#sct_1", "Section 1", heading="Scope"),
        ],
        article_children={},
    )

    units = _adapter().read_native_units(graph)

    ignored_keywords = ("Recital", "Annex", "Chapter", "Section")
    assert len(units) == 1
    assert units[0].citation_ref == "Art. 1"
    assert not any(keyword in unit.citation_ref for unit in units for keyword in ignored_keywords)
