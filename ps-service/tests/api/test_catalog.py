"""Unit tests for the curated regulation catalog (`ps_service.api.catalog`)."""

from __future__ import annotations

from ps_service.api.catalog import (
    CATALOG,
    REGULATION_CATALOG,
    CatalogEntry,
    find_by_celex,
    find_short_name_collision,
)


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


# --- find_short_name_collision (issue #146, D3.1) ----------------------------


def test_find_short_name_collision_returns_none_when_unclaimed() -> None:
    """A ``short_name`` no curated entry uses at all collides with nothing."""
    assert find_short_name_collision("not-a-real-short-name", "32099R9999") is None


def test_find_short_name_collision_returns_the_conflicting_entry() -> None:
    """A different curated entry already claiming ``short_name`` is returned as the conflict.

    ``celex`` is the *caller's* CELEX, matching neither fixture entry's own CELEX --
    the entry sharing ``short_name`` under a differing CELEX is the collision. Passes
    its own fixture tuple via ``find_short_name_collision``'s ``catalog=`` parameter
    (AUDIT.md §2 case 11) rather than monkeypatching the module-level
    ``REGULATION_CATALOG`` constant -- the real curated catalog can never itself
    produce a same-``short_name`` collision (by construction, proven by
    ``test_no_two_curated_entries_share_a_short_name`` below), so a test needing one
    supplies its own catalog through the real, dedicated DI seam instead.
    """
    fixture = (
        CatalogEntry("32024R0001", "Fixture One", "shared-name", "1.0"),
        CatalogEntry("32024R0002", "Fixture Two", "shared-name", "1.0"),
    )

    conflict = find_short_name_collision("shared-name", "32024R9999", catalog=fixture)

    assert conflict == fixture[0]


def test_no_two_curated_entries_share_a_short_name() -> None:
    """Real-catalog invariant: no two distinct curated entries share a ``short_name``.

    Regression guard for AC-BI-006 -- if this ever fails, a new curation entry
    was added with a ``short_name`` already claimed by another entry.
    """
    seen: dict[str, str] = {}
    for entry in REGULATION_CATALOG:
        assert entry.short_name not in seen, (
            f"short_name {entry.short_name!r} is shared by CELEX {seen.get(entry.short_name)} "
            f"and {entry.celex}"
        )
        seen[entry.short_name] = entry.celex
