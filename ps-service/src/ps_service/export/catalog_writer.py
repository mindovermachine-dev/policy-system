"""ps_service.export.catalog_writer -- `manifest.json`/`catalog.json` I/O (D1).

`write_manifest` writes one curated instrument's `manifest.json`;
`read_manifest` is its inverse (used by `export_instrument` to rebuild the
full catalog listing after every curation run). `write_catalog_json` writes
the repo-root, aggregate `catalog.json` listing -- and, per CHANGES.md's MA3
fix, an identical second copy at a caller-supplied `packaged_copy_path` so
`ps_service.api.curated_content`'s `importlib.resources`-packaged copy never
drifts from the git-tracked, `ps-cli`-facing one (both are written from the
same computed JSON string, in the same call). Both files are always
regenerated wholesale -- never hand-edited, never merged with stale content
(D1).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from ps_service.export.models import InstrumentManifest

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

__all__ = ["read_manifest", "write_catalog_json", "write_manifest"]

_MANIFEST_FILENAME = "manifest.json"
_CATALOG_FILENAME = "catalog.json"


class _CatalogEntry(TypedDict):
    """One `catalog.json` row -- AC-BI-011/012's data source (D1)."""

    instrument_id: str
    celex: str | None
    title: str
    source_type: Literal["external", "internal"]
    jurisdiction: str | None
    short_name: str
    version: str


def write_manifest(instrument_dir: Path, manifest: InstrumentManifest) -> None:
    """Write `manifest.json` into `instrument_dir` (D1).

    Creates `instrument_dir` if missing. Overwrites any existing
    `manifest.json` wholesale -- a curation run always writes a complete,
    fresh manifest, never a partial update.
    """
    instrument_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(manifest), sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    (instrument_dir / _MANIFEST_FILENAME).write_text(text, encoding="utf-8")


def read_manifest(instrument_dir: Path) -> InstrumentManifest:
    """Read `instrument_dir / manifest.json` back into an `InstrumentManifest`.

    The exact inverse of `write_manifest` -- every field `write_manifest`
    serializes is read back unchanged, field-for-field.
    """
    document = cast(
        "dict[str, object]", json.loads((instrument_dir / _MANIFEST_FILENAME).read_text("utf-8"))
    )
    return InstrumentManifest(**document)  # pyright: ignore[reportArgumentType] -- manifest.json is this module's own trusted output, not external input


def _catalog_entry(manifest: InstrumentManifest) -> _CatalogEntry:
    return {
        "instrument_id": manifest.instrument_id,
        "celex": manifest.celex,
        "title": manifest.title,
        "source_type": manifest.source_type,
        "jurisdiction": manifest.jurisdiction,
        "short_name": manifest.short_name,
        "version": manifest.version,
    }


def write_catalog_json(
    repo_root: Path,
    packaged_copy_path: Path,
    manifests: Iterable[InstrumentManifest],
) -> None:
    """Write the aggregate `catalog.json` listing to both its destinations.

    One JSON array, one entry per manifest, sorted by `instrument_id`
    (deterministic diff for future curation PRs) -- computed once as a
    single string and written unchanged to both `repo_root / "catalog.json"`
    (the canonical, git-tracked, `ps-cli`-facing copy, D1/D3) and
    `packaged_copy_path` (CHANGES.md MA3's `importlib.resources`-packaged
    copy `api/catalog.py` reads inside the built container image), so
    both stay in sync by construction. Replaces each destination file
    wholesale every call -- never merges with whatever was there before.
    """
    entries = sorted(
        (_catalog_entry(manifest) for manifest in manifests),
        key=lambda entry: entry["instrument_id"],
    )
    text = json.dumps(entries, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    for destination in (repo_root / _CATALOG_FILENAME, packaged_copy_path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
