"""The pure ``CatalogEntry`` value type shared by the ingest path and the catalog reader.

It lives apart from ``ps_service.api.catalog`` so the ingest path, which builds one for every
Cellar-resolved CELEX, can use the type without importing the module that reads
``catalog.json``. ``ps_service.api.catalog`` re-exports it, so other importers need not change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One curated EU regulatory instrument that carries a CELEX identifier.

    This is the CELEX-only contract of ``GET /regulations`` and ``POST /ingestions``. The ingest
    path also builds it positionally, as ``CatalogEntry(celex, title, short_name, version)``,
    for a CELEX resolved through Cellar. An internal-source instrument has no CELEX and is never
    a :class:`CatalogEntry`; the unfiltered ``GET /catalog`` shape is
    ``ps_service.api.catalog.CuratedInstrumentEntry``.

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
