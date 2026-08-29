"""Live parser-parity check: base-act XHTML vs its consolidated expression.

`@pytest.mark.cellar_live` on every test (via `pytestmark`) — real network
calls to `publications.europa.eu`, excluded from the fast suite and run
explicitly:

    uv run pytest ps-service/tests/ingestion/adapters/cellar_eli/test_structure_live.py \
        -m cellar_live -q

Purpose (Follow-on A, folded into issue #19): prove that `parse_structure`
extracts the *same* structural skeleton from the Official Journal's
consolidated rendering as from the original base act — same ARTICLE count,
same ANNEX count, and (today) the same PARAGRAPH count — with the preamble
(RECITAL) legitimately absent from the consolidation.

Measured counts, captured from live fetches on 2026-08-29 (mirrors the
captured-response convention in `test_cellar_consolidated.py`):

| CELEX (base / consolidated)        | ARTICLE | PARAGRAPH | ANNEX | RECITAL |
|------------------------------------|---------|-----------|-------|---------|
| 32016R0679  (GDPR base)            |      99 |       372 |     0 |     173 |
| 02016R0679-20160504 (GDPR consol.) |      99 |       372 |     0 |       0 |
| 32024R2847  (CRA base)             |      71 |       288 |     8 |     130 |
| 02024R2847-20241120 (CRA consol.)  |      71 |       288 |     8 |       0 |

PARAGRAPH parity is asserted as exact equality: it holds *today* only
because no amendment has renumbered or inserted a paragraph in GDPR or CRA.
A future consolidation that does so would legitimately diverge; if this
assertion ever fails on that basis, loosen it to `>= base` for the
affected CELEX (the ANNEX / ARTICLE skeleton is the load-bearing parity
claim — see FLAWS_A finding 6 / PLAN_A §5 risk 4).
"""

from __future__ import annotations

from collections import Counter

import pytest

from ps_service.ingestion.adapters.cellar_eli.fetch import fetch_xhtml
from ps_service.ingestion.adapters.cellar_eli.metadata import extract_metadata
from ps_service.ingestion.adapters.cellar_eli.structure import (
    ANNEX,
    ARTICLE,
    PARAGRAPH,
    RECITAL,
    parse_structure,
)

pytestmark = [pytest.mark.cellar_live]

# (base-act CELEX, consolidated-expression CELEX, expected base-act celex).
_PARITY_CASES = [
    pytest.param("32016R0679", "02016R0679-20160504", id="gdpr"),
    pytest.param("32024R2847", "02024R2847-20241120", id="cra"),
]


def _counts_by_type(xhtml: bytes, regulatory_instrument_id: str) -> Counter[str]:
    nodes, _edges = parse_structure(xhtml, regulatory_instrument_id)
    return Counter(node.element_type for node in nodes)


@pytest.mark.parametrize(("base_celex", "consolidated_celex"), _PARITY_CASES)
def test_consolidated_structure_matches_base_act_skeleton(
    base_celex: str, consolidated_celex: str
) -> None:
    """`parse_structure` yields the same ARTICLE / ANNEX / PARAGRAPH
    skeleton for the consolidated expression as for the base act, and the
    consolidation carries no preamble (0 RECITAL) while the base act does.
    """
    base_counts = _counts_by_type(fetch_xhtml(base_celex), f"{base_celex}-base")
    consolidated_counts = _counts_by_type(
        fetch_xhtml(consolidated_celex), f"{consolidated_celex}-consol"
    )

    assert consolidated_counts[ARTICLE] == base_counts[ARTICLE]
    assert consolidated_counts[ANNEX] == base_counts[ANNEX]
    assert consolidated_counts[PARAGRAPH] == base_counts[PARAGRAPH]

    assert base_counts[RECITAL] > 0
    assert consolidated_counts[RECITAL] == 0


@pytest.mark.parametrize(("base_celex", "consolidated_celex"), _PARITY_CASES)
def test_consolidated_article_headings_do_not_leak_into_article_text(
    base_celex: str, consolidated_celex: str
) -> None:
    """The consolidated rendering labels articles with a
    `title-article-norm` paragraph; that label must be suppressed, never
    folded into the ARTICLE node's own text.
    """
    nodes, _edges = parse_structure(fetch_xhtml(consolidated_celex), f"{consolidated_celex}-consol")

    leaked = [
        node.id
        for node in nodes
        if node.element_type == ARTICLE
        and str(node.properties.get("text", "")).startswith("Article ")
    ]
    assert leaked == []


@pytest.mark.parametrize(("base_celex", "consolidated_celex"), _PARITY_CASES)
def test_consolidated_structural_node_ids_are_scoped_to_the_instrument(
    base_celex: str, consolidated_celex: str
) -> None:
    """Every structural node id is prefixed with the passed
    `regulatory_instrument_id`, so a base-act subtree and a consolidated
    subtree for the same instrument never collide in `{short}_native`.
    """
    regulatory_instrument_id = f"{consolidated_celex}-consol"
    nodes, _edges = parse_structure(fetch_xhtml(consolidated_celex), regulatory_instrument_id)

    assert nodes
    assert all(node.id.startswith(f"{regulatory_instrument_id}#") for node in nodes)


@pytest.mark.parametrize(("base_celex", "consolidated_celex"), _PARITY_CASES)
def test_consolidated_metadata_carries_the_base_act_celex(
    base_celex: str, consolidated_celex: str
) -> None:
    """`extract_metadata` normalises a consolidated CELEX back to its
    base-act form, so a re-ingested consolidation stays pollable by
    `poll_for_amendments`.
    """
    metadata = extract_metadata(fetch_xhtml(consolidated_celex), consolidated_celex)

    assert metadata.celex == base_celex
    assert metadata.instrument_type == "regulation"
