"""Tests for ps_service.ingestion.adapters.cellar_eli.adapter.

The main test here (`test_...through_one_instance`) is the concrete
proof-of-pluggability test for AC-001/AC-006 at the adapter level: one
`CellarEliAdapter` instance, one injected `fetch` callable capable of
serving multiple identifiers (exactly mirroring how the real `fetch_xhtml`
serves any CELEX value), called twice with two different identifiers —
correctly-different results, with no code change or branching on the
identifier's value anywhere in `adapter.py`.
"""

from __future__ import annotations

import inspect
from datetime import date

import pytest

from ps_service.ingestion.adapters.base import IngestionAdapter
from ps_service.ingestion.adapters.cellar_eli.adapter import CellarEliAdapter
from ps_service.ingestion.adapters.cellar_eli.fetch import fetch_xhtml
from ps_service.ingestion.adapters.errors import CellarFetchError, CellarParseError

# Fixture A: a Regulation-shaped document (Entry-into-force wording, no
# recital, no annex).
_FIXTURE_REGULATION_A = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Regulation (EU) 1111/1111 Fixture A</div>
<div class="eli-subdivision" id="cpt_I">
<div class="eli-title" id="cpt_I.tit_1">CHAPTER I General provisions</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Entry into force and application</div>
<div>This Regulation shall enter into force on the twentieth day following
publication. It shall apply from 1 January 2030.</div>
</div>
</div>
</div>
</body>
</html>
"""

# Fixture B: a Directive-shaped document (Transposition wording, plus a
# recital and a top-level annex — deliberately different structure/counts
# from Fixture A, not just different text).
_FIXTURE_DIRECTIVE_B = b"""
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<div class="eli-container" id="enc_1">
<div class="eli-main-title">Directive (EU) 2222/2222 Fixture B</div>
<div class="eli-subdivision" id="cpt_I">
<div class="eli-title" id="cpt_I.tit_1">CHAPTER I General provisions</div>
<div class="eli-subdivision" id="art_1">
<div class="eli-title" id="art_1.tit_1">Article 1 Transposition</div>
<div>By 1 January 2031, Member States shall adopt and publish the measures
necessary to comply with this Directive.</div>
</div>
</div>
<div class="eli-subdivision" id="rct_1">Whereas this Directive is necessary for the internal market.</div>
</div>
<div class="eli-container" id="anx_I">
<div class="eli-title" id="anx_I.tit_1">ANNEX I Technical requirements</div>
<div>Requirements listed below.</div>
</div>
</body>
</html>
"""

_FIXTURES_BY_IDENTIFIER = {
    "32020R1111": _FIXTURE_REGULATION_A,
    "32020L2222": _FIXTURE_DIRECTIVE_B,
}


def _dispatching_fetch(identifier: str) -> bytes:
    """A single fetch function capable of serving multiple identifiers —
    exactly what the real `fetch_xhtml(celex)` does for any CELEX value.
    Injected once per test; the dispatch-by-identifier lives here, in test
    fakery, never in `adapter.py`'s own source."""
    return _FIXTURES_BY_IDENTIFIER[identifier]


def test_default_fetch_is_the_real_fetch_xhtml() -> None:
    signature = inspect.signature(CellarEliAdapter.__init__)
    assert signature.parameters["fetch"].default is fetch_xhtml


def test_satisfies_ingestion_adapter_protocol() -> None:
    adapter: IngestionAdapter = CellarEliAdapter(fetch=_dispatching_fetch)

    result = adapter.fetch_regulation_structure("32020R1111")

    assert result.metadata.title == "Regulation (EU) 1111/1111 Fixture A"


def test_fetch_regulation_structure_produces_different_results_for_different_identifiers_through_one_instance() -> (
    None
):
    adapter = CellarEliAdapter(fetch=_dispatching_fetch)

    result_a = adapter.fetch_regulation_structure("32020R1111")
    result_b = adapter.fetch_regulation_structure("32020L2222")

    # Different metadata.
    assert result_a.metadata.title == "Regulation (EU) 1111/1111 Fixture A"
    assert result_b.metadata.title == "Directive (EU) 2222/2222 Fixture B"
    assert result_a.metadata.effective_date == date(2030, 1, 1)
    assert result_b.metadata.effective_date == date(2031, 1, 1)
    assert result_a.metadata.instrument_type == "regulation"
    assert result_b.metadata.instrument_type == "directive"

    # Different structure: Fixture A has no recital/annex, Fixture B does.
    assert len(result_a.nodes) == 2  # CHAPTER, ARTICLE
    assert len(result_b.nodes) == 4  # CHAPTER, ARTICLE, RECITAL, ANNEX
    assert {node.element_type for node in result_a.nodes} == {"CHAPTER", "ARTICLE"}
    assert {node.element_type for node in result_b.nodes} == {"CHAPTER", "ARTICLE", "RECITAL", "ANNEX"}

    # `identifier` flows through unmodified as the structural-node-id
    # prefix — proof the adapter passed it straight through, not a
    # hardcoded/derived value.
    assert all(node.id.startswith("32020R1111#") for node in result_a.nodes)
    assert all(node.id.startswith("32020L2222#") for node in result_b.nodes)


def test_fetch_regulation_structure_propagates_cellar_fetch_error_unchanged() -> None:
    def _failing_fetch(identifier: str) -> bytes:
        raise CellarFetchError(f"could not fetch {identifier}")

    adapter = CellarEliAdapter(fetch=_failing_fetch)

    with pytest.raises(CellarFetchError):
        adapter.fetch_regulation_structure("32020R1111")


def test_fetch_regulation_structure_propagates_cellar_parse_error_unchanged() -> None:
    _unresolvable_effective_date_fixture = b"""
    <html xmlns="http://www.w3.org/1999/xhtml">
    <body>
    <div class="eli-container" id="enc_1">
    <div class="eli-main-title">Some Regulation</div>
    <div class="eli-subdivision" id="art_1">
    <div class="eli-title" id="art_1.tit_1">Article 1 Subject matter</div>
    <div id="001.001">This Regulation establishes rules.</div>
    </div>
    </div>
    </body>
    </html>
    """

    def _fetch(identifier: str) -> bytes:
        return _unresolvable_effective_date_fixture

    adapter = CellarEliAdapter(fetch=_fetch)

    with pytest.raises(CellarParseError):
        adapter.fetch_regulation_structure("32020R1111")
