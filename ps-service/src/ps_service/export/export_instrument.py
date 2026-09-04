"""ps_service.export.export_instrument -- the D4 curation-export orchestrator.

`export_instrument` is the one function the maintainer-only
`tools/curated-export/` scripts call to turn an already-ingested
`{short}_baseline`/`{short}_native` FalkorDB graph pair into one curated
instrument directory (`curated-content/{instrument_id}/`) plus a
regenerated `catalog.json` (D1). Fixed orchestration order (D7,
CHANGES2.md §3.9): embeddings are backfilled onto the live baseline graph
first, then both graphs are serialized to JSON, then the manifest is
written, then `catalog.json` is regenerated from every manifest now on disk
(never just this one instrument's) -- so a re-export never drops another
instrument's catalog entry.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export import catalog_writer
from ps_service.export.embeddings import backfill_capability_embeddings
from ps_service.export.models import InstrumentManifest
from ps_service.export.serialize import checksum_bytes, serialize_graph, to_json_bytes

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.llm_interface.client import EmbeddingCaller
    from ps_service.logging.emitter import LogEmitter

__all__ = ["InstrumentDescriptor", "export_instrument"]

_CURATED_CONTENT_DIRNAME = "curated-content"
_BASELINE_FILENAME = "baseline.json"
_NATIVE_FILENAME = "native.json"


@dataclass(frozen=True, slots=True)
class InstrumentDescriptor:
    """The manifest-describing identity fields for one curated instrument.

    Groups `export_instrument`'s seven manifest-identity parameters into one
    argument -- this repo's `max-args = 8` rule (root `pyproject.toml`)
    would otherwise be tripped by `export_instrument`'s full parameter list
    (descriptor fields plus `baseline_graph`/`native_graph`/`embed_model`/
    `repo_root`/`packaged_copy_path`/`call_embedding`/`emitter`). Mirrors
    `ps_service.restore.staging.StagedLegNames`'s own precedent for the
    identical problem: a grouping dataclass, not a `# noqa` suppression.
    """

    short_name: str
    instrument_id: str
    version: str
    celex: str | None
    title: str
    source_type: Literal["external", "internal"]
    jurisdiction: str | None


def _read_all_manifests(curated_content_dir: Path) -> list[InstrumentManifest]:
    """Read every already-written `manifest.json` under `curated_content_dir`.

    Used to regenerate `catalog.json` from the complete, current set of
    curated instruments -- never just the one this call just exported.
    """
    if not curated_content_dir.is_dir():
        return []
    return [
        catalog_writer.read_manifest(instrument_dir)
        for instrument_dir in sorted(curated_content_dir.iterdir())
        if (instrument_dir / "manifest.json").is_file()
    ]


def export_instrument(
    descriptor: InstrumentDescriptor,
    *,
    baseline_graph: _GraphQueryHandle,
    native_graph: _GraphQueryHandle,
    embed_model: str,
    repo_root: Path,
    packaged_copy_path: Path,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> InstrumentManifest:
    """Curate one instrument end to end: embed, serialize, write, catalog.

    Caller has already selected `baseline_graph`/`native_graph` (the live
    `{short}_baseline`/`{short}_native` graphs on an already-ingested source
    FalkorDB instance) -- this function does no graph selection of its own.
    Writes `curated-content/{descriptor.instrument_id}/{manifest.json,
    baseline.json,native.json}` under `repo_root`, and regenerates
    `catalog.json` at both `repo_root / "curated-content" / "catalog.json"`
    and `packaged_copy_path` (CHANGES.md MA3).

    Returns the written `InstrumentManifest`.
    """
    backfill_capability_embeddings(
        baseline_graph,
        source_type=descriptor.source_type,
        model=embed_model,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    baseline_bytes = to_json_bytes(serialize_graph(baseline_graph))
    native_bytes = to_json_bytes(serialize_graph(native_graph))

    manifest = InstrumentManifest(
        instrument_id=descriptor.instrument_id,
        celex=descriptor.celex,
        title=descriptor.title,
        short_name=descriptor.short_name,
        version=descriptor.version,
        source_type=descriptor.source_type,
        jurisdiction=descriptor.jurisdiction,
        schema_version=DOMAIN_SCHEMA_VERSION,
        exported_at=datetime.datetime.now(tz=datetime.UTC).isoformat(),
        baseline_sha256=checksum_bytes(baseline_bytes),
        native_sha256=checksum_bytes(native_bytes),
    )

    curated_content_dir = repo_root / _CURATED_CONTENT_DIRNAME
    instrument_dir = curated_content_dir / descriptor.instrument_id
    instrument_dir.mkdir(parents=True, exist_ok=True)
    (instrument_dir / _BASELINE_FILENAME).write_bytes(baseline_bytes)
    (instrument_dir / _NATIVE_FILENAME).write_bytes(native_bytes)
    catalog_writer.write_manifest(instrument_dir, manifest)

    catalog_writer.write_catalog_json(
        curated_content_dir, packaged_copy_path, _read_all_manifests(curated_content_dir)
    )

    return manifest
