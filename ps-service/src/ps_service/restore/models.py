"""Core types for `ps_service.restore`: the artifact a restore admits and its outcome.

Depends on `ps_service.export.models.InstrumentManifest` one-directionally: Restore depends on
Export's manifest shape; Export never imports from Restore. Plain frozen dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ps_service.export.models import InstrumentManifest


@dataclass(frozen=True, slots=True)
class RestoreArtifact:
    """One curated instrument's artifact, read off disk and ready to restore.

    The blobs are read verbatim and are not parsed until checksum verification and the
    `schema_version` comparison have passed; `export.serialize.parse_serialized_graph_json` is
    the parse step, called by `restore_instrument`.

    Attributes:
        manifest: The instrument's manifest: ids, `schema_version` and the SHA-256 digests the
            blobs are verified against.
        baseline_blob: UTF-8 JSON bytes of the baseline graph (`baseline.json`, as written by
            `export.serialize.to_json_bytes`).
        native_blob: UTF-8 JSON bytes of the native graph (`native.json`, same format).
    """

    manifest: InstrumentManifest
    baseline_blob: bytes
    native_blob: bytes


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """The result of a completed `restore_instrument` call.

    Only a successful restore returns one; a failed restore raises instead.

    Attributes:
        instrument_id: Canonical id of the restored instrument (non-empty).
        stages: Names, in order, of the restore stages that ran; an audit-facing summary, not a
            control-flow signal.
        near_miss_reviews_created: Number of Capability `PendingReview` nodes the restore
            recorded for a Compliance Officer to resolve; 0 when there were none.
    """

    instrument_id: str
    stages: tuple[str, ...]
    near_miss_reviews_created: int
