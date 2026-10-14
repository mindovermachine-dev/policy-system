"""Shared artifact-construction helper for `ps_service.restore`'s test package.

`tests/restore` is an importable package (it has an `__init__.py`), so its
per-file `falkordb_live` tests share this one `RestoreArtifact`-construction
helper instead of each redefining the same manifest/checksum plumbing --
mirrors `tests/company_merge/_fakes.py`'s own "shared helpers for an
importable test package" convention.
"""

from __future__ import annotations

from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import (
    InstrumentManifest,
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
)
from ps_service.export.serialize import checksum_bytes, to_json_bytes
from ps_service.restore.models import RestoreArtifact

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
    exercises `restore_instrument`'s real checksum check, never a
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


def small_graphs(
    *, instrument_id: str = "GW214-1.0", capability_name: str = "Keep Logs"
) -> tuple[SerializedGraph, SerializedGraph]:
    """A small external instrument as `(native, baseline)`: enough to reach every leg and edge kind.

    Native: the instrument and one article. Baseline: the instrument, a Role it defines, a
    Requirement it expresses, an Obligation the Role has and the Requirement is satisfied by, and
    a Capability the Obligation requires.
    """

    def edge(
        kind: str, source: tuple[str, str], target: tuple[str, str], **properties: str
    ) -> SerializedEdge:
        return SerializedEdge(kind, source[0], source[1], target[0], target[1], dict(properties))

    instrument = SerializedNode("RegulatoryInstrument", {"id": instrument_id, "title": "GW214"})
    native = SerializedGraph(
        nodes=(instrument, SerializedNode("ARTICLE", {"id": "art_1", "text": "Article 1"})),
        edges=(edge("HAS", ("RegulatoryInstrument", instrument_id), ("ARTICLE", "art_1")),),
    )
    baseline = SerializedGraph(
        nodes=(
            instrument,
            SerializedNode("Role", {"id": "role_1", "name": "Operator", "confidence": 0.9}),
            SerializedNode(
                "Requirement",
                {
                    "id": "req_1",
                    "text": "Must log.",
                    "type": "requirement",
                    "confidence": 0.9,
                    "role_id": "role_1",
                },
            ),
            SerializedNode("Obligation", {"id": "obl_1", "text": "Log it.", "confidence": 0.9}),
            SerializedNode(
                "Capability", {"id": "cap_1", "name": capability_name, "confidence": 0.8}
            ),
        ),
        edges=(
            edge(
                "DEFINES",
                ("RegulatoryInstrument", instrument_id),
                ("Role", "role_1"),
                source_ref="Art. 1",
            ),
            edge(
                "EXPRESSES",
                ("RegulatoryInstrument", instrument_id),
                ("Requirement", "req_1"),
                source_ref="Art. 1",
            ),
            edge("HAS", ("Role", "role_1"), ("Obligation", "obl_1")),
            edge("SATISFIED_BY", ("Requirement", "req_1"), ("Obligation", "obl_1")),
            edge("REQUIRES", ("Obligation", "obl_1"), ("Capability", "cap_1")),
        ),
    )
    return native, baseline
