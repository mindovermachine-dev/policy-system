"""Core types for `ps_service.restore` -- the artifact restore admits.

Depends on `ps_service.export.models.InstrumentManifest` -- one-directional
(Restore depends on Export's manifest shape; Export never imports from
Restore). Plain frozen dataclasses, matching `ps_service.export.models`'s own
convention for internal-pipeline-plumbing shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ps_service.export.models import InstrumentManifest


@dataclass(frozen=True, slots=True)
class RestoreArtifact:
    """One curated instrument's artifact, read off disk and ready to restore.

    `baseline_blob`/`native_blob` are UTF-8 JSON bytes (`export.serialize.
    to_json_bytes`'s output), read verbatim from `baseline.json`/
    `native.json` (PLAN.md D1, CHANGES2.md §2.1/§3.1) -- never parsed until
    AFTER checksum verification (D9) and schema_version comparison (D10);
    `export.serialize.parse_serialized_graph_json` is the parse step,
    called by `restore_instrument` between D10 and D8 step 2.
    """

    manifest: InstrumentManifest
    baseline_blob: bytes
    native_blob: bytes


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """The result of a completed `restore_instrument` call.

    `stages` records, in order, which stages of D8's staged-write sequence
    actually ran -- an audit-facing summary, not a control-flow signal.
    """

    instrument_id: str
    stages: tuple[str, ...]
