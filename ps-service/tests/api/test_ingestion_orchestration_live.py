r"""Live verification: `resolve_via_cellar` against the real Cellar/ELI service.

`@pytest.mark.cellar_live` on every test in this module (via `pytestmark`):
real network calls to `publications.europa.eu` -- excluded from the fast
regression suite (`-m "not cellar_live"`) and must be run explicitly:

    uv run pytest ps-service/tests/api/test_ingestion_orchestration_live.py \
        -m cellar_live -q

Purpose: `resolve_via_cellar` (`ps_service.api.ingestion_orchestration`) is
unit-tested against fixture XHTML; this module re-proves its two load-bearing
claims against genuine Cellar/ELI data instead of a fake:

1. A real, well-formed CELEX for a real regulation that is *not* in the
   curated `REGULATION_CATALOG` (`ps_service.api.catalog`) resolves
   successfully, with a real title-derived `short_name` and `version ==
   "1.0"`.
2. The document is fetched at most once per resolution+Stage-1 pair -- proven
   here with a counting wrapper around the *real* `fetch_xhtml`, not a fake,
   so a defect only real Cellar XHTML structure could expose (e.g. a second
   hidden fetch inside `extract_metadata` or `parse_structure`) would
   actually be caught.

CELEX used: `32023R1114`, Regulation (EU) 2023/1114 (the Markets in
Crypto-Assets Regulation / MiCA) -- real, well-formed
(`^3\\d{4}[A-Z]\\d{4}$`), type `R` (regulation), and confirmed absent from
`REGULATION_CATALOG` (`ps_service/api/catalog.py`) as of 2026-09-27.

The CELEX must stay absent from the curated catalog: once a regulation is
curated into `REGULATION_CATALOG` it does not exercise the non-curated
premise
(`test_celex_under_test_is_genuinely_absent_from_the_curated_catalog`
guards this), so the CELEX has to be swapped for another real,
Cellar-resolvable, non-curated regulation.
"""

from __future__ import annotations

import pytest

from ps_service.api.catalog import REGULATION_CATALOG
from ps_service.api.ingestion_orchestration import resolve_via_cellar
from ps_service.ingestion.adapters.cellar_eli.fetch import fetch_xhtml

pytestmark = [pytest.mark.cellar_live]

_NONCURATED_CELEX = "32023R1114"


class _CountingFetch:
    """Wraps the real `fetch_xhtml`, counting how many times it is called.

    Deliberately a *wrapper* around the live function, not a replacement --
    the point is to prove "exactly one real HTTP call" against genuine
    Cellar/ELI, matching the counting-fake technique of the unit tests but with
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
    """The fetch-once proof, against real infrastructure: a
    counting wrapper around the real `fetch_xhtml` is passed explicitly via
    `cellar_fetch=`. Simulating Stage 1 by calling
    `resolution.adapter.fetch_regulatory_instrument_structure(celex)` directly
    (the same technique the fake-based tests use) must not trigger a
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
