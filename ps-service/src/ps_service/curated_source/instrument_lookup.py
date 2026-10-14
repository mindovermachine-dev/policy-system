"""Resolve a requested `instrument_id` against the catalog case-insensitively.

GitHub raw paths are case-sensitive, so the fetch itself cannot be made case-insensitive.
Resolution therefore happens against the already-fetched catalog
(`ps_service.curated_source.catalog_client.fetch_catalog`) first, and the canonical
(catalog-cased) id is then used for the artifact fetch. The module is a single pure function
with no I/O.
"""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

from ps_service.curated_source.errors import (
    CuratedSourceAmbiguousInstrumentIdError,
    CuratedSourceUnknownInstrumentIdError,
)

if TYPE_CHECKING:
    from ps_service.api.catalog import CuratedInstrumentEntry

_CLOSEST_MATCH_COUNT = 3
_CLOSEST_MATCH_CUTOFF = 0.0


def resolve_canonical_instrument_id(
    catalog: tuple[CuratedInstrumentEntry, ...], requested_id: str
) -> str:
    """Resolve `requested_id` to the catalog's canonical spelling, ignoring case.

    Args:
        catalog: Every curated instrument entry, as returned by
            `ps_service.curated_source.catalog_client.fetch_catalog`. May be empty, which makes
            every lookup fail.
        requested_id: The caller's own spelling of the instrument id, for example `"cra-1.0"`.

    Returns:
        The matching entry's own canonical-cased `instrument_id`, for example `"CRA-1.0"`.

    Raises:
        CuratedSourceAmbiguousInstrumentIdError: More than one catalog entry matches
            `requested_id` case-insensitively. The message names `requested_id` and every
            colliding canonical id.
        CuratedSourceUnknownInstrumentIdError: No catalog entry matches in any case. The message
            names `requested_id` and up to 3 closest candidate ids ranked by similarity
            (`difflib.get_close_matches` with `cutoff=0.0`, so candidates are named even when
            none is close); an empty catalog is reported as such.
    """
    requested_lower = requested_id.lower()
    matches = [entry for entry in catalog if entry.instrument_id.lower() == requested_lower]
    if len(matches) == 1:
        return matches[0].instrument_id
    if len(matches) > 1:
        colliding_ids = ", ".join(entry.instrument_id for entry in matches)
        message = (
            f"instrument id {requested_id!r} matches more than one catalog entry "
            f"case-insensitively: {colliding_ids}"
        )
        raise CuratedSourceAmbiguousInstrumentIdError(message)
    original_case_by_lower = {entry.instrument_id.lower(): entry.instrument_id for entry in catalog}
    closest_lower = difflib.get_close_matches(
        requested_lower,
        original_case_by_lower.keys(),
        n=_CLOSEST_MATCH_COUNT,
        cutoff=_CLOSEST_MATCH_CUTOFF,
    )
    closest_ids = [original_case_by_lower[lowered] for lowered in closest_lower]
    message = (
        f"no catalog entry matches instrument id {requested_id!r} in any case; "
        f"closest ids: {', '.join(closest_ids) if closest_ids else '(catalog is empty)'}"
    )
    raise CuratedSourceUnknownInstrumentIdError(message)
