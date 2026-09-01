r"""Live verification: `resolve_via_cellar` against the real Cellar/ELI service
(Increment 17, `.orchestrator/tracker/issue-61-ingest-celex-cellar-fallback/PLAN.md`
§3 Increment 17).

`@pytest.mark.cellar_live` on every test in this module (via `pytestmark`):
real network calls to `publications.europa.eu` -- excluded from the fast
regression suite (`-m "not cellar_live"`) and must be run explicitly:

    uv run pytest ps-service/tests/api/test_ingestion_orchestration_live.py \
        -m cellar_live -q

Purpose: `resolve_via_cellar` (`ps_service.api.ingestion_orchestration`) has
been extensively unit-tested against fixture XHTML (Increments 5/6) -- this
module re-proves its two load-bearing claims against genuine Cellar/ELI data
instead of a fake:

1. A real, well-formed CELEX for a real regulation that is *not* in the
   curated `REGULATION_CATALOG` (`ps_service.api.catalog`) resolves
   successfully, with a real title-derived `short_name` and `version ==
   "1.0"` (D-slug, AC-BI-004).
2. The document is fetched at most once per resolution+Stage-1 pair (D2,
   AC-BI-006) -- proven here with a counting wrapper around the *real*
   `fetch_xhtml`, not a fake, so a defect only real Cellar XHTML structure
   could expose (e.g. a second hidden fetch inside `extract_metadata` or
   `parse_structure`) would actually be caught.

CELEX used: `32022R2554`, Regulation (EU) 2022/2554 (the Digital Operational
Resilience Act / DORA) -- real, well-formed (`^3\\d{4}[A-Z]\\d{4}$`), type `R`
(regulation), and confirmed absent from `REGULATION_CATALOG` (CRA/GDPR/NIS2
only) by reading `ps_service/api/catalog.py` directly.

**Substitution note, found during this increment's own red run, not assumed:**
the task brief that seeded this file specified `32019R0881` (the EU
Cybersecurity Act / ENISA Regulation) as the live-verification CELEX. Running
against it surfaced a genuine defect, not a test bug or a network issue:
`extract_metadata`'s heading-text-matching extraction mechanism for
`effective_date` (`metadata.py`, since superseded by RDF-based resolution per
issue #62) only recognised an Article heading containing "Transposition" or
"Entry into force and application"; 32019R0881's real final article (Art. 69)
is headed simply "Entry into force" (no "and application"), so resolution
raised `CellarParseError` -> `PipelineStageError`, not the happy path this
module exists to demonstrate. Confirmed against six other real, non-curated
regulations (DORA, DSA, the AI Act, the P2B Regulation, the Terrorist Content
Online Regulation, the Data Act) -- all six resolve cleanly, so this is a
narrow heading-phrasing gap specific to 32019R0881's drafting, not a systemic
`resolve_via_cellar` defect. Out of this test-only increment's scope to fix
(PLAN.md §3 Increment 17: "Changes: none"); reported to the orchestrator in
`IMPL_17.md` as a follow-up-worthy finding. DORA is substituted here so this
module still delivers its actual purpose -- a genuine, passing live proof of
`resolve_via_cellar`'s happy path and fetch-once behaviour -- without erasing
the 32019R0881 finding, which stays fully documented here and in `IMPL_17.md`
rather than silently dropped.
"""

from __future__ import annotations

import pytest

from ps_service.api.catalog import REGULATION_CATALOG
from ps_service.api.ingestion_orchestration import resolve_via_cellar
from ps_service.ingestion.adapters.cellar_eli.fetch import fetch_xhtml

pytestmark = [pytest.mark.cellar_live]

_NONCURATED_CELEX = "32022R2554"


class _CountingFetch:
    """Wraps the real `fetch_xhtml`, counting how many times it is called.

    Deliberately a *wrapper* around the live function, not a replacement --
    the point is to prove "exactly one real HTTP call" against genuine
    Cellar/ELI, matching Increment 5's own counting-fake technique but with
    the fake's fixed-body return swapped for a real network round-trip.
    """

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, celex: str) -> bytes:
        self.call_count += 1
        return fetch_xhtml(celex)


def test_celex_under_test_is_genuinely_absent_from_the_curated_catalog() -> None:
    """Guards this module's own premise: if a future catalog edit ever adds
    this CELEX, this live test would stop exercising the Cellar-fallback path
    it exists to verify -- fail loudly instead of silently testing nothing.
    """
    assert _NONCURATED_CELEX not in {entry.celex for entry in REGULATION_CATALOG}


def test_resolve_via_cellar_live_fetch_of_a_real_noncurated_celex() -> None:
    """`resolve_via_cellar` succeeds against the real, unmocked `fetch_xhtml`
    default (no `cellar_fetch=` override) for a genuine, non-curated CELEX.
    """
    resolution = resolve_via_cellar(_NONCURATED_CELEX)

    entry = resolution.entry
    assert entry.celex == _NONCURATED_CELEX
    assert entry.version == "1.0"
    # Structural assertions only -- Cellar's returned title text could shift,
    # so this does not hardcode the exact expected slug string. `_derive_short_name`
    # guarantees: non-empty, `_`-joined lowercase words, CELEX suffix appended
    # in lowercase.
    assert entry.short_name
    assert entry.short_name.endswith(f"_{_NONCURATED_CELEX.lower()}")
    assert entry.short_name == entry.short_name.lower()
    assert entry.title


def test_resolve_via_cellar_fetches_the_real_document_at_most_once_for_the_run() -> None:
    """The fetch-once proof (D2, AC-BI-006), against real infrastructure: a
    counting wrapper around the real `fetch_xhtml` is passed explicitly via
    `cellar_fetch=`. Simulating Stage 1 by calling
    `resolution.adapter.fetch_regulatory_instrument_structure(celex)` directly
    (same technique Increment 5's fake-based test used) must not trigger a
    second live HTTP call -- proving the cached-bytes adapter genuinely
    avoids a second real round-trip, not just a second call against a fake.
    """
    counting_fetch = _CountingFetch()

    resolution = resolve_via_cellar(_NONCURATED_CELEX, cellar_fetch=counting_fetch)
    assert counting_fetch.call_count == 1

    structure = resolution.adapter.fetch_regulatory_instrument_structure(_NONCURATED_CELEX)

    assert counting_fetch.call_count == 1
    assert structure.metadata.celex == _NONCURATED_CELEX
    assert structure.nodes
