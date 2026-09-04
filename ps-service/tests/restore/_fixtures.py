"""Shared artifact-construction helper for `ps_service.restore`'s test package.

`tests/restore` is an importable package (it has an `__init__.py`), so its
per-file `falkordb_live` tests share this one `RestoreArtifact`-construction
helper instead of each redefining the same manifest/checksum plumbing --
mirrors `tests/company_merge/_fakes.py`'s own "shared helpers for an
importable test package" convention.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import InstrumentManifest
from ps_service.export.serialize import checksum_bytes, to_json_bytes
from ps_service.restore.models import RestoreArtifact

if TYPE_CHECKING:
    from ps_service.export.models import SerializedGraph

_EXPORTED_AT = "2026-09-04T00:00:00Z"


def build_restore_artifact(
    *,
    instrument_id: str,
    short_name: str,
    native_graph: SerializedGraph,
    baseline_graph: SerializedGraph,
    schema_version: str = DOMAIN_SCHEMA_VERSION,
) -> RestoreArtifact:
    """Build a checksum-correct `RestoreArtifact` from two already-built `SerializedGraph`s.

    Encodes both graphs via the real `export.serialize.to_json_bytes` (the
    same codec `export_instrument` itself uses) and computes each blob's
    real SHA-256 via `checksum_bytes`, so every test using this helper
    exercises `restore_instrument`'s real D9 checksum check, never a
    pre-faked digest.
    """
    native_blob = to_json_bytes(native_graph)
    baseline_blob = to_json_bytes(baseline_graph)
    manifest = InstrumentManifest(
        instrument_id=instrument_id,
        celex=None,
        title=instrument_id,
        short_name=short_name,
        version="1.0",
        source_type="external",
        jurisdiction=None,
        schema_version=schema_version,
        exported_at=_EXPORTED_AT,
        baseline_sha256=checksum_bytes(baseline_blob),
        native_sha256=checksum_bytes(native_blob),
    )
    return RestoreArtifact(manifest=manifest, baseline_blob=baseline_blob, native_blob=native_blob)
