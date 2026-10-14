"""Read and write the curated catalog's `manifest.json` and `catalog.json` files.

`write_manifest` writes one curated instrument's `manifest.json`; `read_manifest` is its
inverse, used to rebuild the full catalog listing after every curation run.
`write_catalog_json` writes the aggregate `catalog.json` listing to the repository root and an
identical copy to a caller-supplied packaged path, so the copy shipped inside the container
image (read through `importlib.resources` by `ps_service.api.curated_content`) cannot drift from
the git-tracked copy that clients read. Both files are always regenerated wholesale, never
hand-edited and never merged with earlier content.
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
    """One `catalog.json` row: the manifest fields a catalog listing exposes per instrument."""

    instrument_id: str
    celex: str | None
    title: str
    source_type: Literal["external", "internal"]
    jurisdiction: str | None
    short_name: str
    version: str


def write_manifest(instrument_dir: Path, manifest: InstrumentManifest) -> None:
    """Write one instrument's `manifest.json` into its directory.

    A curation run always writes a complete manifest, never a partial update, so an existing
    file is replaced wholesale. The JSON uses sorted keys, two-space indent, UTF-8 and a
    trailing newline so repeated runs produce identical bytes.

    Args:
        instrument_dir: Directory that holds the instrument's artifact files. Created, with
            parents, when missing.
        manifest: The complete manifest to serialize; every dataclass field is written.

    Returns:
        None.

    Side Effects:
        Creates `instrument_dir` if needed and creates or overwrites
        `instrument_dir / "manifest.json"`.
    """
    instrument_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(manifest), sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    (instrument_dir / _MANIFEST_FILENAME).write_text(text, encoding="utf-8")


def read_manifest(instrument_dir: Path) -> InstrumentManifest:
    """Read an instrument's `manifest.json` back into an `InstrumentManifest`.

    The exact inverse of `write_manifest`: every field it serializes is read back unchanged.
    The file is trusted to be this module's own output and is not schema-validated.

    Args:
        instrument_dir: Directory that holds `manifest.json`.

    Returns:
        The manifest, field for field as written.

    Raises:
        FileNotFoundError: `instrument_dir` has no `manifest.json`.
        json.JSONDecodeError: The file is not valid JSON.
        TypeError: The JSON keys do not match the `InstrumentManifest` fields.
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
    """Write the aggregate `catalog.json` listing to both of its destinations.

    The listing is one JSON array with one entry per manifest, sorted by `instrument_id` so
    diffs between curation runs stay small. It is computed once as a single string and written
    unchanged to `repo_root / "catalog.json"` (the git-tracked copy clients read) and to
    `packaged_copy_path` (the copy packaged into the container image), so the two cannot
    differ. Each destination is replaced wholesale on every call.

    Args:
        repo_root: Directory that receives `catalog.json` (git-tracked copy);
            `export_instrument` passes the `curated-content` directory.
        packaged_copy_path: Full file path of the second copy. Its parent directories are
            created when missing.
        manifests: The manifests to list, in any order. An empty iterable writes `[]`.

    Returns:
        None.

    Side Effects:
        Creates or overwrites both destination files and any missing parent directories.
    """
    entries = sorted(
        (_catalog_entry(manifest) for manifest in manifests),
        key=lambda entry: entry["instrument_id"],
    )
    text = json.dumps(entries, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    for destination in (repo_root / _CATALOG_FILENAME, packaged_copy_path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
