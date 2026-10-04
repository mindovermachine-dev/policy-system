"""Tests for ``ps_service.curated_source.instrument_lookup.resolve_canonical_instrument_id``.

Issue #184: a requested ``instrument_id`` is resolved against the catalog
case-insensitively before any artifact fetch. Pure, fast, Detroit-style unit
tests -- no I/O, no fakes, a plain `CuratedInstrumentEntry` tuple built
directly per test (mirrors `ps_service.api.catalog`'s own
`find_by_celex`/`find_short_name_collision` precedent of accepting a
test-supplied `catalog` tuple for scenarios the real curated catalog can't
produce).
"""

from __future__ import annotations

import pytest

from ps_service.api.catalog import CuratedInstrumentEntry
from ps_service.curated_source.errors import (
    CuratedSourceAmbiguousInstrumentIdError,
    CuratedSourceUnknownInstrumentIdError,
)
from ps_service.curated_source.instrument_lookup import resolve_canonical_instrument_id


def _entry(instrument_id: str) -> CuratedInstrumentEntry:
    return CuratedInstrumentEntry(
        instrument_id=instrument_id,
        celex="32024R2847",
        title="Cyber Resilience Act",
        source_type="external",
        jurisdiction="EU",
        short_name="CRA",
        version="1.0",
    )


def test_exact_case_match_returns_the_canonical_id() -> None:
    catalog = (_entry("CRA-1.0"),)

    assert resolve_canonical_instrument_id(catalog, "CRA-1.0") == "CRA-1.0"


def test_lowercase_request_against_uppercase_catalog_entry_returns_the_canonical_id() -> None:
    """The real bug case (GH #184): `cra-1.0` resolves to the catalog's own `CRA-1.0`."""
    catalog = (_entry("CRA-1.0"),)

    assert resolve_canonical_instrument_id(catalog, "cra-1.0") == "CRA-1.0"


def test_ambiguous_case_collision_raises_naming_both_colliding_ids() -> None:
    catalog = (_entry("CRA-1.0"), _entry("cra-1.0"))

    with pytest.raises(CuratedSourceAmbiguousInstrumentIdError) as exc_info:
        resolve_canonical_instrument_id(catalog, "CRA-1.0")

    message = str(exc_info.value)
    assert "CRA-1.0" in message
    assert "cra-1.0" in message


def test_no_match_in_any_case_raises_naming_closest_candidate_ids() -> None:
    catalog = (_entry("CRA-1.0"), _entry("GDPR-1.0"))

    with pytest.raises(CuratedSourceUnknownInstrumentIdError) as exc_info:
        resolve_canonical_instrument_id(catalog, "cra-9.9")

    message = str(exc_info.value)
    assert "cra-9.9" in message
    assert "CRA-1.0" in message


def test_empty_catalog_raises_not_found_without_crashing() -> None:
    with pytest.raises(CuratedSourceUnknownInstrumentIdError) as exc_info:
        resolve_canonical_instrument_id((), "cra-1.0")

    assert "cra-1.0" in str(exc_info.value)
