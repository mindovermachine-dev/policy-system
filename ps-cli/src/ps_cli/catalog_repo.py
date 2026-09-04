"""ps_cli.catalog_repo -- local-filesystem reads of the curated-content repo (D13, Slice 7.1).

`read_catalog()`/`read_artifact()` never touch PS Service over the network at
all -- they read `curated_repo_path`'s on-disk `catalog.json` (`catalog
list`) and one instrument's `manifest.json` + `baseline.json` + `native.json`
(`catalog restore`, before uploading their bytes to PS Service). Vendors its
own lightweight shapes rather than importing `ps_service.export.models.
InstrumentManifest` -- L2 Common's "ps-service and ps-cli are fully
decoupled ... vendors its own copy of anything it needs" rule, the same
convention `ps_cli.models` already establishes for PS Service's REST
response shapes (`ps-cli/tests/test_architecture_boundary.py` structurally
enforces "never import ps_service.*", confirmed read before this module was
written). Per CHANGES2.md §3.7, `read_artifact` returns the two blobs as raw
`bytes` -- parsing their JSON content into a graph happens server-side, so
`ps-cli` never needs to understand `SerializedGraph`'s shape at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from ps_cli.errors import PsCliError, assert_contract

if TYPE_CHECKING:
    from pathlib import Path

_CATALOG_FILENAME = "catalog.json"
_MANIFEST_FILENAME = "manifest.json"
_BASELINE_FILENAME = "baseline.json"
_NATIVE_FILENAME = "native.json"

_UNEXPECTED_CATALOG_SHAPE_MSG = "curated catalog.json has an unexpected shape"
_UNEXPECTED_MANIFEST_SHAPE_MSG = "curated instrument's manifest.json has an unexpected shape"


@dataclass(frozen=True)
class CuratedInstrumentEntry:
    """One curated instrument as listed in the local `catalog.json` (D1)."""

    instrument_id: str
    title: str
    source_type: str
    jurisdiction: str | None


@dataclass(frozen=True)
class CuratedInstrumentManifest:
    """One curated instrument's `manifest.json` fields (D1).

    Field-for-field mirror of `ps_service.export.models.InstrumentManifest`
    -- vendored, not imported, per this module's own docstring.
    """

    instrument_id: str
    celex: str | None
    title: str
    short_name: str
    version: str
    source_type: str
    jurisdiction: str | None
    schema_version: str
    exported_at: str
    baseline_sha256: str
    native_sha256: str


@dataclass(frozen=True)
class CuratedArtifact:
    """One curated instrument's artifact, read off local disk (Slice 7.1).

    `baseline_blob`/`native_blob` are the raw UTF-8 JSON bytes read verbatim
    from `baseline.json`/`native.json` -- never parsed here (CHANGES2.md
    §3.7: parsing into a `SerializedGraph` happens server-side, only after
    PS Service verifies the artifact's checksums/schema_version).
    """

    manifest: CuratedInstrumentManifest
    baseline_blob: bytes
    native_blob: bytes


def _parse_catalog_entry(payload: object) -> CuratedInstrumentEntry:
    """Parse one raw JSON object from `catalog.json` into a `CuratedInstrumentEntry`.

    Raises `PsCliError` (generic, defensive) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_CATALOG_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    instrument_id = body.get("instrument_id")
    title = body.get("title")
    source_type = body.get("source_type")
    jurisdiction = body.get("jurisdiction")
    if (
        not isinstance(instrument_id, str)
        or not isinstance(title, str)
        or not isinstance(source_type, str)
        or not (jurisdiction is None or isinstance(jurisdiction, str))
    ):
        raise PsCliError(msg=_UNEXPECTED_CATALOG_SHAPE_MSG)
    return CuratedInstrumentEntry(
        instrument_id=instrument_id,
        title=title,
        source_type=source_type,
        jurisdiction=jurisdiction,
    )


def read_catalog(repo_path: Path) -> list[CuratedInstrumentEntry]:
    """Read and parse `repo_path / "catalog.json"` into `CuratedInstrumentEntry` objects.

    Local filesystem only -- never touches PS Service (D13: `catalog list`
    needs no PS Service connection at all, unlike `catalog restore`). Raises
    `PsCliError` naming the missing path if `catalog.json` does not exist,
    or a generic `PsCliError` if its content does not match the expected
    shape.
    """
    catalog_path = repo_path / _CATALOG_FILENAME
    assert_contract(
        contract=catalog_path.is_file(),
        msg=f"curated catalog not found: {catalog_path}",
        hint="check curated_repo_path (ps-cli.toml / PS_CLI_CURATED_REPO_PATH)",
    )
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise PsCliError(msg=_UNEXPECTED_CATALOG_SHAPE_MSG)
    entries = cast("list[object]", payload)
    return [_parse_catalog_entry(entry) for entry in entries]


def _parse_manifest(payload: object) -> CuratedInstrumentManifest:
    """Parse `manifest.json`'s content into a `CuratedInstrumentManifest`.

    Raises `PsCliError` (generic, defensive) if the shape does not match.
    """
    if not isinstance(payload, dict):
        raise PsCliError(msg=_UNEXPECTED_MANIFEST_SHAPE_MSG)
    body = cast("dict[str, object]", payload)
    instrument_id = body.get("instrument_id")
    celex = body.get("celex")
    title = body.get("title")
    short_name = body.get("short_name")
    version = body.get("version")
    source_type = body.get("source_type")
    jurisdiction = body.get("jurisdiction")
    schema_version = body.get("schema_version")
    exported_at = body.get("exported_at")
    baseline_sha256 = body.get("baseline_sha256")
    native_sha256 = body.get("native_sha256")
    if (
        not isinstance(instrument_id, str)
        or not (celex is None or isinstance(celex, str))
        or not isinstance(title, str)
        or not isinstance(short_name, str)
        or not isinstance(version, str)
        or not isinstance(source_type, str)
        or not (jurisdiction is None or isinstance(jurisdiction, str))
        or not isinstance(schema_version, str)
        or not isinstance(exported_at, str)
        or not isinstance(baseline_sha256, str)
        or not isinstance(native_sha256, str)
    ):
        raise PsCliError(msg=_UNEXPECTED_MANIFEST_SHAPE_MSG)
    return CuratedInstrumentManifest(
        instrument_id=instrument_id,
        celex=celex,
        title=title,
        short_name=short_name,
        version=version,
        source_type=source_type,
        jurisdiction=jurisdiction,
        schema_version=schema_version,
        exported_at=exported_at,
        baseline_sha256=baseline_sha256,
        native_sha256=native_sha256,
    )


def read_artifact(repo_path: Path, instrument_id: str) -> CuratedArtifact:
    """Read one curated instrument's manifest + both blobs off local disk.

    Raises `PsCliError` naming the missing path if `repo_path / instrument_id`
    is not a directory. `baseline_blob`/`native_blob` are returned as raw
    `bytes`, read verbatim from `baseline.json`/`native.json` -- never
    parsed here (CHANGES2.md §3.7: parsing happens server-side, after PS
    Service verifies the artifact).
    """
    instrument_dir = repo_path / instrument_id
    assert_contract(
        contract=instrument_dir.is_dir(),
        msg=f"curated instrument directory not found: {instrument_dir}",
        hint=f"check the instrument id, or that {repo_path} is a real curated-content checkout",
    )
    manifest_payload = json.loads((instrument_dir / _MANIFEST_FILENAME).read_text(encoding="utf-8"))
    manifest = _parse_manifest(manifest_payload)
    baseline_blob = (instrument_dir / _BASELINE_FILENAME).read_bytes()
    native_blob = (instrument_dir / _NATIVE_FILENAME).read_bytes()
    return CuratedArtifact(manifest=manifest, baseline_blob=baseline_blob, native_blob=native_blob)
