"""Curated catalog of instruments offered by ``GET /catalog``/``GET /regulations``.

The catalog is read from ``catalog.json`` (D1's aggregate curated-instrument
listing) via :func:`load_regulation_catalog` -- CHANGES.md MA3's
``importlib.resources``-packaged copy, mirroring
``ps_service.mcp_interface.mcp_server``'s ``_domain_concepts_path()``
precedent exactly (``mcp_server.py:59-68``), so it serves correctly from
inside a built container image (the repo-root ``curated-content/`` tree never
reaches that image's build context, MA3) and not only from a repo checkout.

:data:`CATALOG` is every curated entry, external and internal, unfiltered
(AC-BI-011's ``GET /catalog`` listing). :data:`REGULATION_CATALOG` is the
existing, narrower ``GET /regulations``/``POST /ingestions`` contract --
CELEX-carrying (external) entries only (D12) -- computed from :data:`CATALOG`
so both stay derived from the one source of truth, never independently
hand-maintained. ``short_name``/``version`` stay internal to
:class:`CatalogEntry` -- they drive per-regulation graph naming and the
``f"{short_name}-{version}"`` RegulatoryInstrument id downstream -- while the
``GET /regulations`` HTTP response exposes only ``celex`` and ``title``.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Iterable
    from importlib.resources.abc import Traversable
    from pathlib import Path


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
        short_name: Internal short name driving graph naming, e.g. ``"CRA"``.
        version: Internal catalog version forming the RegulatoryInstrument id
            ``f"{short_name}-{version}"``.
    """

    celex: str
    title: str
    short_name: str
    version: str


@dataclass(frozen=True, slots=True)
class CuratedInstrumentEntry:
    """One curated instrument, unfiltered -- external regulation or internal source.

    The full ``catalog.json`` row shape (D1/D12/MA3): every curated
    instrument, external and internal, with no CELEX-only narrowing. Feeds
    :data:`CATALOG` (``GET /catalog``'s AC-BI-011 listing);
    :data:`REGULATION_CATALOG` derives its narrower, CELEX-only
    :class:`CatalogEntry` view from this.

    Attributes:
        instrument_id: The curated directory id, e.g. ``"CRA-1.0"`` (D1).
        celex: The 10-character CELEX identifier, or ``None`` for an
            ``internal``-sourced instrument (no CELEX applies, D15).
        title: The human-readable instrument title.
        source_type: ``"external"`` (an EU regulation) or ``"internal"``
            (a project-authored source, D15).
        jurisdiction: ``"EU"`` for an external regulation, or ``None`` for an
            internal source with no jurisdiction concept.
        short_name: Internal short name driving graph naming, e.g. ``"CRA"``.
        version: Internal catalog version forming the RegulatoryInstrument id
            ``f"{short_name}-{version}"``.
    """

    instrument_id: str
    celex: str | None
    title: str
    source_type: Literal["external", "internal"]
    jurisdiction: str | None
    short_name: str
    version: str


@functools.cache
def _catalog_json_path() -> Traversable:
    """Packaged location of ``catalog.json`` (CHANGES.md MA3, AC-BI-012).

    Resolved via ``importlib.resources`` against this installed package, not
    a repo-checkout-relative path -- so the resource serves correctly from a
    wheel/container install with no repo checkout present, not only from an
    editable/dev install. Mirrors ``ps_service.mcp_interface.mcp_server.
    _domain_concepts_path()`` (``mcp_server.py:59-68``) exactly. Lazy +
    cached: never touched at import.
    """
    return resources.files("ps_service.api.curated_content").joinpath("catalog.json")


def load_regulation_catalog(
    catalog_path: Path | Traversable | None = None,
) -> tuple[CuratedInstrumentEntry, ...]:
    """Read every curated instrument entry from ``catalog.json``, unfiltered.

    Args:
        catalog_path: An explicit path (or ``importlib.resources``
            ``Traversable``) to read -- tests pass a ``tmp_path`` fixture.
            When ``None`` (the default), reads the packaged production copy
            via :func:`_catalog_json_path`.

    Returns:
        Every entry in ``catalog.json``, external and internal, in file
        order, as :class:`CuratedInstrumentEntry` objects with every field
        populated.
    """
    source = catalog_path if catalog_path is not None else _catalog_json_path()
    raw = cast("Iterable[dict[str, object]]", json.loads(source.read_text(encoding="utf-8")))
    return tuple(
        CuratedInstrumentEntry(
            instrument_id=cast("str", item["instrument_id"]),
            celex=cast("str | None", item["celex"]),
            title=cast("str", item["title"]),
            source_type=cast('Literal["external", "internal"]', item["source_type"]),
            jurisdiction=cast("str | None", item["jurisdiction"]),
            short_name=cast("str", item["short_name"]),
            version=cast("str", item["version"]),
        )
        for item in raw
    )


CATALOG: tuple[CuratedInstrumentEntry, ...] = load_regulation_catalog()
"""Every curated instrument, external and internal, unfiltered -- AC-BI-011's ``GET /catalog``
listing source."""

REGULATION_CATALOG: tuple[CatalogEntry, ...] = tuple(
    CatalogEntry(
        celex=entry.celex, title=entry.title, short_name=entry.short_name, version=entry.version
    )
    for entry in CATALOG
    if entry.celex is not None
)
"""CELEX-carrying (external) entries only -- ``GET /regulations``'s/``POST /ingestions``'s
existing, unchanged contract (D12). Derived from :data:`CATALOG`, never independently loaded."""


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
