r"""Cellar/ELI Domain Mapping Adapter, paired 1:1 with #14's Cellar/ELI Ingestion Adapter.

Paired with `ps_service.ingestion.adapters.cellar_eli.structure`, reading
the exact native-graph shape that component writes.

PLAN_REVIEWED.md §4.2: one `ExtractionUnit` per `PARAGRAPH` when an
`ARTICLE` has them, one per whole `ARTICLE` when it doesn't. Query pattern
(verbatim, per the plan): `MATCH (a:ARTICLE) RETURN a.id, a.citation_ref,
a.text, a.heading ORDER BY a.id`, then per-article `MATCH (:ARTICLE {id:
$id})-[:HAS]->(p:PARAGRAPH) RETURN p.citation_ref, p.text ORDER BY p.order`.

`citation_ref` shape confirmed directly against `ps_service.ingestion.
adapters.cellar_eli.structure`'s own writes (`_walk_article`/
`_mint_paragraph`): `f"Art. {number}"` for an Article's own `citation_ref`,
`f"Art. {article_number}({para_number})"` for a Paragraph's — the plan's
regexes (`^Art\\.\\s*(\\d+)$`, `^Art\\.\\s*(\\d+)\\((\\d+)\\)$`) match this
exactly, no adjustment needed.

Scope decision (Open Question 4, revised by GH #25): `ARTICLE`/`PARAGRAPH`
AND `ANNEX` are queried — `RECITAL`/`CHAPTER`/`SECTION` nodes remain
structurally unreachable from this adapter's Cypher (it never matches those
labels), so they can never contribute an `ExtractionUnit` even when present
in the same native graph. An `ANNEX` node has no `PARAGRAPH`-shaped child
and no `heading` property (unlike `ARTICLE`) — it always becomes exactly one
whole-node `ExtractionUnit`, with `article_heading=""` (never invented) and
`paragraph_number="1"` (the same sentinel the paragraph-less-Article
fallback already uses). `ANNEX` units are always ordered after every
ARTICLE/PARAGRAPH unit, sorted by the native `order` property server-side
(`ORDER BY a.order`), never re-derived from the citation label.

Document order is `(article_number, paragraph_number)` as integers, not the
raw query order (`ORDER BY a.id` sorts lexicographically — `"art_10"` sorts
before `"art_2"` as a string, which is not document order) — the adapter
re-sorts numerically before returning.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from ps_service.domain_mapper.errors import DomainMapperExtractionError
from ps_service.domain_mapper.models import ExtractionUnit

if TYPE_CHECKING:
    from ps_service.domain_mapper.falkordb_client import GraphHandle

_ARTICLE_CITATION_RE = re.compile(r"^Art\.\s*(\d+)$")
_PARAGRAPH_CITATION_RE = re.compile(r"^Art\.\s*(\d+)\((\d+)\)$")
# Negative lookahead rejects a bare-digit annex label (e.g. "Annex 1") while still
# accepting Roman numerals ("I", "II") or any other non-digit-only alphanumeric label —
# a digit-only label would otherwise produce an `article_number` indistinguishable from
# a real numeric-Article-derived one (GH #25 CHANGES.md Row #1).
_ANNEX_CITATION_RE = re.compile(r"^Annex\s+(?!\d+$)(\S+)$")


class CellarEliDomainMappingAdapter:
    """Cellar/ELI implementation of `DomainMappingAdapter` (structural, Protocol).

    Reads `ARTICLE`/`PARAGRAPH` nodes from a Cellar/ELI-ingested native
    structural graph and returns the ordered sequence of extraction units.
    """

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]:
        """Return the graph's extraction units in document order (article, then paragraph)."""
        article_rows = cast(
            "list[list[object]]",
            graph.query(
                "MATCH (a:ARTICLE) RETURN a.id, a.citation_ref, a.text, a.heading ORDER BY a.id"
            ).result_set,
        )

        units: list[ExtractionUnit] = []
        for row in article_rows:
            units.extend(self._units_for_article(graph, row))

        units.sort(key=lambda unit: (int(unit.article_number), int(unit.paragraph_number)))

        annex_rows = cast(
            "list[list[object]]",
            graph.query(
                "MATCH (a:ANNEX) RETURN a.citation_ref, a.text ORDER BY a.order"
            ).result_set,
        )
        annex_units = [self._annex_unit(row) for row in annex_rows]

        return (*units, *annex_units)

    def _units_for_article(self, graph: GraphHandle, row: list[object]) -> list[ExtractionUnit]:
        article_id = cast("str", row[0])
        citation_ref = cast("str", row[1])
        text = cast("str", row[2])
        heading = cast("str", row[3])

        article_match = _ARTICLE_CITATION_RE.match(citation_ref)
        if article_match is None:
            raise DomainMapperExtractionError(
                f"unexpected Article citation_ref shape: {citation_ref!r}"
            )
        article_number = article_match.group(1)

        paragraph_rows = cast(
            "list[list[object]]",
            graph.query(
                "MATCH (:ARTICLE {id: $id})-[:HAS]->(p:PARAGRAPH) "
                "RETURN p.citation_ref, p.text ORDER BY p.order",
                params={"id": article_id},
            ).result_set,
        )

        if not paragraph_rows:
            return [ExtractionUnit(citation_ref, text, article_number, "1", heading)]

        return [self._paragraph_unit(paragraph_row, heading) for paragraph_row in paragraph_rows]

    def _paragraph_unit(self, paragraph_row: list[object], heading: str) -> ExtractionUnit:
        para_citation_ref = cast("str", paragraph_row[0])
        para_text = cast("str", paragraph_row[1])

        paragraph_match = _PARAGRAPH_CITATION_RE.match(para_citation_ref)
        if paragraph_match is None:
            raise DomainMapperExtractionError(
                f"unexpected Paragraph citation_ref shape: {para_citation_ref!r}"
            )
        return ExtractionUnit(
            para_citation_ref,
            para_text,
            paragraph_match.group(1),
            paragraph_match.group(2),
            heading,
        )

    def _annex_unit(self, row: list[object]) -> ExtractionUnit:
        citation_ref = cast("str", row[0])
        text = cast("str", row[1])

        annex_match = _ANNEX_CITATION_RE.match(citation_ref)
        if annex_match is None:
            raise DomainMapperExtractionError(
                f"unexpected Annex citation_ref shape: {citation_ref!r}"
            )
        return ExtractionUnit(citation_ref, text, annex_match.group(1), "1", "")
