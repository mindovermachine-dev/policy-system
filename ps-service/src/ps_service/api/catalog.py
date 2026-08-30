"""Curated static catalog of EU regulatory instruments offered by ``GET /regulations``.

The catalog is a fixed, hand-maintained tuple (not a database read) so the
walking skeleton has a stable, well-known set of CELEX identifiers ``ps-cli``
can drive an ingestion from (AC-BI-001). ``short_name`` and ``version`` stay
internal to :class:`CatalogEntry` — they drive per-regulation graph naming and
the ``f"{short_name}-{version}"`` RegulatoryInstrument id downstream — while the
HTTP response exposes only ``celex`` and ``title``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One curated EU regulatory instrument.

    Attributes:
        celex: The 10-character CELEX identifier, e.g. ``"32024R2847"``.
        title: The human-readable instrument title.
        short_name: Internal short name driving graph naming, e.g. ``"CRA"``.
        version: Internal catalog version forming the RegulatoryInstrument id
            ``f"{short_name}-{version}"``.
    """

    celex: str
    title: str
    short_name: str
    version: str


REGULATION_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("32024R2847", "Cyber Resilience Act", "CRA", "1.0"),
    CatalogEntry("32016R0679", "General Data Protection Regulation", "GDPR", "1.0"),
    CatalogEntry("32022L2555", "NIS2 Directive", "NIS2", "1.0"),
)


def find_by_celex(celex: str) -> CatalogEntry | None:
    """Return the catalog entry whose CELEX equals ``celex``, or ``None`` if absent.

    Args:
        celex: A CELEX identifier to look up (exact string match).

    Returns:
        The matching :class:`CatalogEntry`, or ``None`` when no curated entry
        has that CELEX.
    """
    for entry in REGULATION_CATALOG:
        if entry.celex == celex:
            return entry
    return None
