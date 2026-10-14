"""Curation export: turn an ingested baseline/native graph pair into a curated instrument.

`export_instrument` is the one function the maintainer-only `tools/curated-export/` scripts call
to turn an already-ingested `{short}_baseline`/`{short}_native` FalkorDB graph pair into one
curated instrument directory (`curated-content/{instrument_id}/`) plus a regenerated
`catalog.json`. The order is fixed: embeddings are backfilled onto the live baseline graph first,
then both graphs are serialized to JSON, then the serialized baseline is checked against
`descriptor.instrument_id` (an id that differs from the source graph's own
`RegulatoryInstrument.id` would produce an artifact restore can never load), then the manifest
is written, then `catalog.json` is regenerated from every manifest on disk, not only this
instrument's, so a re-export never drops another instrument's catalog entry.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export import catalog_writer
from ps_service.export.embeddings import backfill_capability_embeddings
from ps_service.export.errors import ExportInstrumentIdMismatchError
from ps_service.export.models import InstrumentManifest
from ps_service.export.serialize import checksum_bytes, serialize_graph, to_json_bytes

if TYPE_CHECKING:
    from pathlib import Path

    from ps_service.export.falkordb_connection import (
        _GraphQueryHandle,  # pyright: ignore[reportPrivateUsage]
    )
    from ps_service.export.models import SerializedGraph
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import EmbeddingCaller
    from ps_service.logging.emitter import LogEmitter

__all__ = ["InstrumentDescriptor", "export_instrument"]

_CURATED_CONTENT_DIRNAME = "curated-content"
_BASELINE_FILENAME = "baseline.json"
_NATIVE_FILENAME = "native.json"


@dataclass(frozen=True, slots=True)
class InstrumentDescriptor:
    """The manifest-describing identity fields for one curated instrument.

    Groups the identity fields that `export_instrument` copies into the manifest, keeping its
    parameter list within the repository's argument-count limit.

    Attributes:
        short_name: Short slug of the instrument; names the `{short}_baseline` and
            `{short}_native` source graphs by convention.
        instrument_id: Id of the curated instrument and name of its directory. Must equal the
            baseline graph's `RegulatoryInstrument.id`.
        version: Version label of the curated content.
        celex: CELEX identifier of an `external` instrument; `None` for `internal` sources.
        title: Human-readable instrument title.
        source_type: `"external"` (ingested regulation) or `"internal"` (internal policy
            content); selects which node labels get embeddings.
        jurisdiction: Jurisdiction label; `None` for `internal` sources.
    """

    short_name: str
    instrument_id: str
    version: str
    celex: str | None
    title: str
    source_type: Literal["external", "internal"]
    jurisdiction: str | None


def _read_all_manifests(curated_content_dir: Path) -> list[InstrumentManifest]:
    """Read every `manifest.json` directly under `curated_content_dir`, sorted by directory name.

    The catalog is regenerated from the complete set of curated instruments, not only the one
    just exported. A missing directory yields an empty list.
    """
    if not curated_content_dir.is_dir():
        return []
    return [
        catalog_writer.read_manifest(instrument_dir)
        for instrument_dir in sorted(curated_content_dir.iterdir())
        if (instrument_dir / "manifest.json").is_file()
    ]


def _validate_instrument_id(
    descriptor: InstrumentDescriptor, baseline_graph: SerializedGraph
) -> None:
    """Raise `ExportInstrumentIdMismatchError` unless the id names a baseline RegulatoryInstrument.

    Restore rejects a manifest whose `instrument_id` differs from the artifact's own
    `RegulatoryInstrument.id`, so the mismatch (for example a CELEX passed where the graph's id
    is `{short_name}-{version}`) is caught before anything is written to disk.
    """
    regulatory_instrument_ids = [
        node.properties.get("id")
        for node in baseline_graph.nodes
        if node.label == "RegulatoryInstrument"
    ]
    if descriptor.instrument_id not in regulatory_instrument_ids:
        raise ExportInstrumentIdMismatchError(
            f"--instrument-id {descriptor.instrument_id!r} does not match the source baseline "
            f"graph's own RegulatoryInstrument.id ({regulatory_instrument_ids!r}). Pass "
            f"--instrument-id matching the graph's actual id -- restore requires these to be equal."
        )


def export_instrument(  # noqa: PLR0913 -- the two graphs, the gateway seam and the output paths; no natural grouping
    descriptor: InstrumentDescriptor,
    *,
    baseline_graph: _GraphQueryHandle,
    native_graph: _GraphQueryHandle,
    embed_model: str,
    repo_root: Path,
    packaged_copy_path: Path,
    submitter: GroupSubmitter,
    audit_event_id: str,
    baseline_graph_name: str,
    call_embedding: EmbeddingCaller | None = None,
    emitter: LogEmitter | None = None,
) -> InstrumentManifest:
    """Curate one instrument end to end: embed, serialize, write files, regenerate the catalog.

    Business Rules:
        `descriptor.instrument_id` must equal the `id` of a `RegulatoryInstrument` node in the
        baseline graph, so the exported artifact can always be restored. The manifest records
        the current `DOMAIN_SCHEMA_VERSION` and the SHA-256 of both serialized graphs.

    Args:
        descriptor: Identity fields of the instrument, copied into the manifest.
        baseline_graph: Read handle on the already-selected live baseline graph; this function
            selects no graph itself.
        native_graph: Read handle on the already-selected live native graph.
        embed_model: Embedding model name used for the backfill.
        repo_root: Repository root; output goes under `repo_root / "curated-content"`.
        packaged_copy_path: Full file path of the second `catalog.json` copy packaged into the
            container image.
        submitter: Graph Write Gateway submitter used for the embedding backfill.
        audit_event_id: Id of the system audit row the caller recorded first; the backfill group
            is anchored to it.
        baseline_graph_name: Name of the baseline graph, passed in rather than derived from
            `descriptor.short_name` because the CLI names its source graph from `--short-name`.
        call_embedding: Embedding provider callable; `None` uses the default provider caller.
        emitter: Log emitter; `None` uses the default emitter.

    Returns:
        The `InstrumentManifest` that was written to `manifest.json`.

    Raises:
        ExportInstrumentIdMismatchError: `descriptor.instrument_id` does not match the baseline
            graph's `RegulatoryInstrument.id`. Raised after the embedding backfill but before any
            file is written.
        ExportGraphWriteError: The embedding backfill did not take effect.
        ExportSourceGraphError: A node or edge endpoint in either graph does not carry exactly
            one label.
        LlmProviderError: An embedding call failed.

    Side Effects:
        Submits an embedding group to the baseline graph through the gateway. Writes
        `curated-content/{instrument_id}/{manifest.json,baseline.json,native.json}` under
        `repo_root` and regenerates `catalog.json` at `repo_root / "curated-content"` and at
        `packaged_copy_path`.
    """
    backfill_capability_embeddings(
        baseline_graph,
        baseline_graph_name=baseline_graph_name,
        submitter=submitter,
        audit_event_id=audit_event_id,
        source_type=descriptor.source_type,
        model=embed_model,
        call_embedding=call_embedding,
        emitter=emitter,
    )

    baseline_serialized = serialize_graph(baseline_graph)
    _validate_instrument_id(descriptor, baseline_serialized)

    baseline_bytes = to_json_bytes(baseline_serialized)
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
