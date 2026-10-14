"""Curated catalog of instruments offered by ``GET /catalog`` and ``GET /regulations``.

The catalog is read from the packaged ``catalog.json`` through :func:`load_regulation_catalog`,
using ``importlib.resources`` so it resolves from inside a built container image as well as from
a repository checkout.

:data:`CATALOG` holds every curated entry, external and internal, unfiltered; it backs
``GET /catalog``. :data:`REGULATION_CATALOG` is the narrower CELEX-carrying (external) view that
backs ``GET /regulations`` and ``POST /ingestions``. It is computed from :data:`CATALOG` so both
derive from one source of truth. ``short_name`` and ``version`` stay internal to
:class:`CatalogEntry`: they drive per-regulation graph naming and the
``f"{short_name}-{version}"`` RegulatoryInstrument id, while the ``GET /regulations`` response
exposes only ``celex`` and ``title``.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Literal, cast

from ps_service.api.catalog_entry import CatalogEntry

if TYPE_CHECKING:
    from collections.abc import Iterable
    from importlib.resources.abc import Traversable
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class CuratedInstrumentEntry:
    """One curated instrument, unfiltered: an external regulation or an internal source.

    This is the full ``catalog.json`` row shape, with no CELEX-only narrowing. It feeds
    :data:`CATALOG` (the ``GET /catalog`` listing), and :data:`REGULATION_CATALOG` derives its
    narrower CELEX-only :class:`CatalogEntry` view from it.

    Attributes:
        instrument_id: The curated directory id, e.g. ``"32024R2847"``.
        celex: The 10-character CELEX identifier, or ``None`` for an
            ``internal``-sourced instrument (no CELEX applies).
        title: The human-readable instrument title.
        source_type: ``"external"`` (an EU regulation) or ``"internal"``
            (a project-authored source).
        jurisdiction: ``"EU"`` for an external regulation, or ``None`` for an
            internal source with no jurisdiction concept.
        short_name: Internal short name driving graph naming, e.g. ``"cra"``.
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
    """Locate the packaged ``catalog.json``.

    Resolved through ``importlib.resources`` against the installed package rather than a
    checkout-relative path, so it works from a wheel or container install. The result is cached
    and nothing is resolved at import time.

    Returns:
        A ``Traversable`` for ``catalog.json`` inside ``ps_service.api.curated_content``.

    Raises:
        ModuleNotFoundError: If the ``ps_service.api.curated_content`` package cannot be
            imported.
    """
    return resources.files("ps_service.api.curated_content").joinpath("catalog.json")


def load_regulation_catalog(
    catalog_path: Path | Traversable | None = None,
) -> tuple[CuratedInstrumentEntry, ...]:
    """Read every curated instrument entry from ``catalog.json``, unfiltered.

    Args:
        catalog_path: Explicit path or ``importlib.resources`` ``Traversable`` of the JSON file
            to read; tests pass a temporary file. ``None`` (the default) reads the packaged
            production copy.

    Returns:
        Every entry in the file, external and internal, in file order, as
        :class:`CuratedInstrumentEntry` objects with every field populated.

    Raises:
        ModuleNotFoundError: If ``catalog_path`` is ``None`` and the packaged
            ``ps_service.api.curated_content`` package cannot be imported.
        OSError: If the file cannot be read.
        UnicodeDecodeError: If the file is not valid UTF-8.
        json.JSONDecodeError: If the file is not valid JSON.
        KeyError: If an entry lacks any of the seven fields of
            :class:`CuratedInstrumentEntry` (``instrument_id``, ``celex``, ``title``,
            ``source_type``, ``jurisdiction``, ``short_name``, ``version``).
        TypeError: If the JSON root or an entry is not the expected list or object.
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
"""Every curated instrument, external and internal, unfiltered; the ``GET /catalog`` source."""

REGULATION_CATALOG: tuple[CatalogEntry, ...] = tuple(
    CatalogEntry(
        celex=entry.celex, title=entry.title, short_name=entry.short_name, version=entry.version
    )
    for entry in CATALOG
    if entry.celex is not None
)
"""CELEX-carrying (external) entries only; the ``GET /regulations`` and ``POST /ingestions`` source.

Derived from :data:`CATALOG`, never loaded independently.
"""


def find_by_celex(
    celex: str, *, catalog: tuple[CatalogEntry, ...] | None = None
) -> CatalogEntry | None:
    """Find the catalog entry for a CELEX identifier.

    Args:
        celex: CELEX identifier to look up; exact string match.
        catalog: Catalog to search. ``None`` (the default) searches the packaged
            :data:`REGULATION_CATALOG`; a caller may pass its own tuple to exercise a scenario
            the real catalog cannot produce, such as a duplicate ``short_name``.

    Returns:
        The first :class:`CatalogEntry` whose ``celex`` equals ``celex``, or ``None`` when no
        entry has that CELEX.
    """
    resolved_catalog = catalog if catalog is not None else REGULATION_CATALOG
    for entry in resolved_catalog:
        if entry.celex == celex:
            return entry
    return None


def find_short_name_collision(
    short_name: str, celex: str, *, catalog: tuple[CatalogEntry, ...] | None = None
) -> CatalogEntry | None:
    """Find a curated entry that already claims a short name under a different CELEX.

    Compares ``CatalogEntry.short_name`` by plain equality. Curated entries carry
    ``short_name`` as its own field, so no id-string prefix needs disambiguating.

    Args:
        short_name: Caller-supplied short name to check.
        celex: CELEX the caller supplied with it. An entry sharing ``short_name`` and this same
            ``celex`` is the same instrument, not a collision.
        catalog: Catalog to search. ``None`` (the default) searches the packaged
            :data:`REGULATION_CATALOG`; a caller may pass its own tuple instead.

    Returns:
        The first entry whose ``short_name`` matches and whose ``celex`` differs, or ``None``
        when there is none.
    """
    resolved_catalog = catalog if catalog is not None else REGULATION_CATALOG
    for entry in resolved_catalog:
        if entry.short_name == short_name and entry.celex != celex:
            return entry
    return None
