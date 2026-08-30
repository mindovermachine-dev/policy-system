"""Unit tests for the curated regulation catalog (`ps_service.api.catalog`)."""

from __future__ import annotations

from ps_service.api.catalog import REGULATION_CATALOG, find_by_celex


def test_catalog_entries_have_ten_char_celex_and_nonempty_title() -> None:
    """AC-BI-001: every curated entry has a 10-character CELEX and a non-empty title."""
    assert len(REGULATION_CATALOG) == 3
    for entry in REGULATION_CATALOG:
        assert len(entry.celex) == 10
        assert entry.title.strip()
        assert entry.short_name.strip()
        assert entry.version.strip()


def test_find_by_celex_returns_none_for_uncurated_identifier() -> None:
    """AC-BI-006: a well-formed but uncurated CELEX resolves to `None`, not a guess."""
    assert find_by_celex("32099R9999") is None


def test_find_by_celex_returns_the_matching_entry_for_a_curated_identifier() -> None:
    """A curated CELEX resolves to exactly its entry."""
    entry = find_by_celex("32016R0679")

    assert entry is not None
    assert entry.title == "General Data Protection Regulation"
    assert entry.short_name == "GDPR"
