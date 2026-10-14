r"""Cellar/ELI Domain Mapping Adapter, paired 1:1 with the Cellar/ELI Ingestion Adapter.

Reads the native-graph shape written by `ps_service.ingestion.adapters.cellar_eli.structure`.

Units: one `ExtractionUnit` per `PARAGRAPH` when an `ARTICLE` has them, one per whole
`ARTICLE` when it has none, and one per `ANNEX`. The read is anchored on the requested
instrument version (`MATCH (:RegulatoryInstrument {id: $rid})-[:HAS*1..]->(a:ARTICLE)`)
because `{short}_native` holds every ingested version side by side.

Citation shapes: an Article's `citation_ref` is `Art. {number}` and a Paragraph's is
`Art. {article_number}({para_number})`; the regexes `^Art\.\s*(\d+)$` and
`^Art\.\s*(\d+)\((\d+)\)$` match exactly these. An Annex's is `Annex {label}`.

Scope: `ARTICLE`, `PARAGRAPH` and `ANNEX` nodes are queried. `RECITAL`, `CHAPTER` and
`SECTION` nodes are never matched, so they never contribute a unit even when present in the
same native graph. An `ANNEX` has no `PARAGRAPH` child and no `heading` property, so it always
becomes exactly one whole-node unit with `article_heading=""` (never invented) and
`paragraph_number="1"` (the same sentinel a paragraph-less Article uses). Annex units come
after every Article and Paragraph unit, ordered by the native `order` property
(`ORDER BY a.order`), never re-derived from the citation label.

Document order is `(article_number, paragraph_number)` as integers, not the raw query order:
`ORDER BY a.id` sorts lexicographically (`"art_10"` before `"art_2"`), so the adapter
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
# The negative lookahead rejects a bare-digit annex label (e.g. "Annex 1") while accepting
# Roman numerals ("I", "II") and any other non-digit-only label: a digit-only label would
# produce an `article_number` indistinguishable from a numeric-Article-derived one.
_ANNEX_CITATION_RE = re.compile(r"^Annex\s+(?!\d+$)(\S+)$")


class CellarEliDomainMappingAdapter:
    """Cellar/ELI implementation of `DomainMappingAdapter` (structural, Protocol).

    Reads `ARTICLE`, `PARAGRAPH` and `ANNEX` nodes from a Cellar/ELI-ingested native graph and
    returns the ordered sequence of extraction units. Holds no state; it issues read-only
    queries only.
    """

    def read_native_units(
        self, graph: GraphHandle, regulatory_instrument_id: str
    ) -> tuple[ExtractionUnit, ...]:
        """Return one instrument version's extraction units in document order.

        Articles and annexes are reached by traversing `HAS` from the
        `RegulatoryInstrument` node named by `regulatory_instrument_id`, so a prior version
        sharing the same `{short}_native` graph contributes nothing.

        Args:
            graph: Read handle on the regulation's native graph (`{short}_native`).
            regulatory_instrument_id: Id of the `RegulatoryInstrument` node to read under.

        Returns:
            Article and Paragraph units sorted numerically by article then paragraph number,
            followed by Annex units in native `order`; empty when the instrument has none.

        Raises:
            DomainMapperExtractionError: If an Article, Paragraph or Annex `citation_ref` does
                not match its expected shape.
        """
        article_rows = cast(
            "list[list[object]]",
            graph.query(
                "MATCH (:RegulatoryInstrument {id: $rid})-[:HAS*1..]->(a:ARTICLE) "
                "RETURN a.id, a.citation_ref, a.text, a.heading ORDER BY a.id",
                params={"rid": regulatory_instrument_id},
            ).result_set,
        )

        units: list[ExtractionUnit] = []
        for row in article_rows:
            units.extend(self._units_for_article(graph, row))

        units.sort(key=lambda unit: (int(unit.article_number), int(unit.paragraph_number)))

        annex_rows = cast(
            "list[list[object]]",
            graph.query(
                "MATCH (:RegulatoryInstrument {id: $rid})-[:HAS*1..]->(a:ANNEX) "
                "RETURN a.citation_ref, a.text ORDER BY a.order",
                params={"rid": regulatory_instrument_id},
            ).result_set,
        )
        annex_units = [self._annex_unit(row) for row in annex_rows]

        return (*units, *annex_units)

    def _units_for_article(self, graph: GraphHandle, row: list[object]) -> list[ExtractionUnit]:
        """Expand one Article row into one unit per Paragraph, or one whole-Article unit."""
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
        """Build a Paragraph unit that inherits its Article's `heading`."""
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
        """Build the single whole-node unit for an Annex row, with an empty heading."""
        citation_ref = cast("str", row[0])
        text = cast("str", row[1])

        annex_match = _ANNEX_CITATION_RE.match(citation_ref)
        if annex_match is None:
            raise DomainMapperExtractionError(
                f"unexpected Annex citation_ref shape: {citation_ref!r}"
            )
        return ExtractionUnit(citation_ref, text, annex_match.group(1), "1", "")
