"""Unit tests for the curated regulation catalog (`ps_service.api.catalog`)."""

from __future__ import annotations

from ps_service.api.catalog import CATALOG, REGULATION_CATALOG, find_by_celex


def test_catalog_entries_have_ten_char_celex_and_nonempty_title() -> None:
    """AC-BI-001: every curated entry has a 10-character CELEX and a non-empty title.

    The entry count is derived from the packaged `catalog.json` (via `CATALOG`), never
    hardcoded: every curation run rewrites that file, so a literal count goes stale on the
    next export. What is asserted is the D12 invariant -- `REGULATION_CATALOG` is exactly
    the CELEX-carrying subset of `CATALOG` -- plus the per-entry shape.
    """
    assert REGULATION_CATALOG, "packaged catalog carries no CELEX-bearing entries"
    assert len(REGULATION_CATALOG) == sum(1 for entry in CATALOG if entry.celex is not None)
    for entry in REGULATION_CATALOG:
        assert len(entry.celex) == 10
        assert entry.title.strip()
        assert entry.short_name.strip()
        assert entry.version.strip()


def test_find_by_celex_returns_none_for_uncurated_identifier() -> None:
    """AC-BI-006: a well-formed but uncurated CELEX resolves to `None`, not a guess."""
    assert find_by_celex("32099R9999") is None


def test_find_by_celex_returns_the_matching_entry_for_a_curated_identifier() -> None:
    """A curated CELEX resolves to exactly its entry -- for every curated CELEX.

    Expectations come from `REGULATION_CATALOG` itself rather than a hardcoded title/short
    name, so a re-curated title (e.g. "GDPR" -> "gdpr") cannot make this test lie about
    lookup correctness.
    """
    for expected in REGULATION_CATALOG:
        assert find_by_celex(expected.celex) == expected
