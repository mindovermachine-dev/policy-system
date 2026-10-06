"""The pure ``CatalogEntry`` value type shared by the ingest path and the catalog reader.

Lives apart from ``ps_service.api.catalog`` (issue #193, CHANGES.md M5) so the
ingest path -- ``ingestion_orchestration``, which builds one for every Cellar-resolved
CELEX -- can use the type without importing the module that reads ``catalog.json``.
``ps_service.api.catalog`` re-exports it, so every other importer is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One curated EU regulatory instrument -- unchanged shape (pre-#66).

    Kept exactly as before (celex required, four fields) for
    ``REGULATION_CATALOG``'s/``find_by_celex``'s/``POST /ingestions``'s
    existing, CELEX-only contract (D12) -- including
    ``ingestion_orchestration.py``'s existing positional Cellar-fallback
    construction, ``CatalogEntry(celex, metadata.title, short_name,
    metadata.version)``, which stays valid unchanged. An internal-source
    instrument (D15, no CELEX at all) is never represented as a
    :class:`CatalogEntry` -- see :class:`CuratedInstrumentEntry` for the
    unfiltered, ``GET /catalog``-facing shape.

    Attributes:
        celex: The 10-character CELEX identifier, e.g. ``"32024R2847"``.
        title: The human-readable instrument title.
        short_name: Internal short name driving graph naming, e.g. ``"cra"``.
        version: Internal catalog version forming the RegulatoryInstrument id
            ``f"{short_name}-{version}"``.
    """

    celex: str
    title: str
    short_name: str
    version: str
