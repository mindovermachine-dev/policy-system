"""Tests for `ps_service.export.export_instrument.export_instrument`
full orchestration, over the gateway rig.

The graphs are the Graph Write Gateway rig's in-memory FalkorDB (a true boundary), read through
`ExportReadGraph` (a `_GraphQueryHandle`); the embedding backfill is written through the rig's
gateway, so `serialize_graph` reads back what the gateway applied. No real FalkorDB, Postgres or
LLM call here (that proof belongs to `test_serialize_live.py` and
`test_engineering_practices_migration_live.py`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from graph_gateway._fakes import GatewayRig
from litellm.types.utils import Embedding, EmbeddingResponse

from export._gateway_fakes import ExportRig
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.export_instrument import InstrumentDescriptor, export_instrument
from ps_service.export.models import InstrumentManifest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from company_merge._fakes import MakeEmitter

    from ps_service.logging import LogEmitter

_CRA_DESCRIPTOR = InstrumentDescriptor(
    short_name="CRA",
    instrument_id="CRA-1.0",
    version="1.0",
    celex="32024R2847",
    title="Cyber Resilience Act",
    source_type="external",
    jurisdiction="EU",
)

_ENGPRAC_DESCRIPTOR = InstrumentDescriptor(
    short_name="ENGPRAC",
    instrument_id="ENGPRAC-2.1",
    version="2.1",
    celex=None,
    title="Engineering Practices Standard",
    source_type="internal",
    jurisdiction=None,
)


class _FakeEmbeddingCaller:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        self.calls.append(inputs[0])
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=self._vector, index=0, object="embedding")]
        )


@pytest.fixture
def rig() -> Iterator[ExportRig]:
    """The gateway rig with a `cra_baseline` / `cra_native` pair (the true boundaries faked)."""
    gateway_rig = GatewayRig()
    export_rig = ExportRig(gateway_rig, short_name="CRA")
    export_rig.seed_node(
        export_rig.baseline, "RegulatoryInstrument", "CRA-1.0", {"title": "Cyber Resilience Act"}
    )
    export_rig.seed_node(
        export_rig.baseline,
        "Capability",
        "cap_1",
        {"name": "Data Encryption", "confidence": 0.9},
    )
    export_rig.seed_node(
        export_rig.native, "RegulatoryInstrument", "CRA-1.0", {"title": "Cyber Resilience Act"}
    )
    yield export_rig
    gateway_rig.close()


def _export(
    rig: ExportRig,
    descriptor: InstrumentDescriptor,
    repo_root: Path,
    packaged_copy_path: Path,
    caller: _FakeEmbeddingCaller,
    emitter: LogEmitter,
) -> InstrumentManifest:
    """Run the real `export_instrument` over `rig`, writing the backfill through its gateway."""
    return export_instrument(
        descriptor,
        baseline_graph=rig.baseline_reader(),  # pyright: ignore[reportArgumentType]
        native_graph=rig.native_reader(),  # pyright: ignore[reportArgumentType]
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=caller,
        emitter=emitter,
        **rig.write_target(),  # pyright: ignore[reportArgumentType]
    )


def test_export_instrument_writes_manifest_and_both_graph_files(
    tmp_path: Path, make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    manifest = _export(
        rig,
        _CRA_DESCRIPTOR,
        repo_root,
        packaged_copy_path,
        _FakeEmbeddingCaller([0.1, 0.2]),
        emitter,
    )

    instrument_dir = repo_root / "curated-content" / "CRA-1.0"
    assert (instrument_dir / "manifest.json").is_file()
    assert (instrument_dir / "baseline.json").is_file()
    assert (instrument_dir / "native.json").is_file()
    assert manifest.instrument_id == "CRA-1.0"
    assert manifest.celex == "32024R2847"
    assert manifest.source_type == "external"
    assert manifest.jurisdiction == "EU"
    assert manifest.schema_version == DOMAIN_SCHEMA_VERSION
    assert len(manifest.baseline_sha256) == 64
    assert len(manifest.native_sha256) == 64


def test_export_instrument_persisted_baseline_json_contains_the_backfilled_embedding(
    tmp_path: Path, make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    _export(
        rig,
        _CRA_DESCRIPTOR,
        repo_root,
        packaged_copy_path,
        _FakeEmbeddingCaller([0.1, 0.2]),
        emitter,
    )

    baseline_path = repo_root / "curated-content" / "CRA-1.0" / "baseline.json"
    baseline_document = json.loads(baseline_path.read_text(encoding="utf-8"))
    capability_nodes = [n for n in baseline_document["nodes"] if n["label"] == "Capability"]
    assert len(capability_nodes) == 1
    assert capability_nodes[0]["properties"]["embedding"] == [0.1, 0.2]


def test_export_instrument_updates_catalog_json_in_both_locations(
    tmp_path: Path, make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    _export(
        rig,
        _CRA_DESCRIPTOR,
        repo_root,
        packaged_copy_path,
        _FakeEmbeddingCaller([0.1, 0.2]),
        emitter,
    )

    repo_catalog = json.loads(
        (repo_root / "curated-content" / "catalog.json").read_text(encoding="utf-8")
    )
    packaged_catalog = json.loads(packaged_copy_path.read_text(encoding="utf-8"))
    assert repo_catalog == packaged_catalog
    assert repo_catalog == [
        {
            "instrument_id": "CRA-1.0",
            "celex": "32024R2847",
            "title": "Cyber Resilience Act",
            "source_type": "external",
            "jurisdiction": "EU",
            "short_name": "CRA",
            "version": "1.0",
        }
    ]


def test_export_instrument_second_export_preserves_the_first_instruments_catalog_entry(
    tmp_path: Path, make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"
    _export(
        rig,
        _CRA_DESCRIPTOR,
        repo_root,
        packaged_copy_path,
        _FakeEmbeddingCaller([0.1, 0.2]),
        emitter,
    )
    engprac = ExportRig(rig.rig, short_name="ENGPRAC")
    for graph in (engprac.baseline, engprac.native):
        engprac.seed_node(
            graph,
            "RegulatoryInstrument",
            "ENGPRAC-2.1",
            {"title": "Engineering Practices Standard"},
        )

    _export(
        engprac,
        _ENGPRAC_DESCRIPTOR,
        repo_root,
        packaged_copy_path,
        _FakeEmbeddingCaller([0.3]),
        emitter,
    )

    catalog = json.loads(
        (repo_root / "curated-content" / "catalog.json").read_text(encoding="utf-8")
    )
    assert [entry["instrument_id"] for entry in catalog] == ["CRA-1.0", "ENGPRAC-2.1"]


def test_export_instrument_backfills_embeddings_before_serializing_the_baseline_graph(
    tmp_path: Path, make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"
    embedding_caller = _FakeEmbeddingCaller([0.1, 0.2])

    _export(rig, _CRA_DESCRIPTOR, repo_root, packaged_copy_path, embedding_caller, emitter)

    statements = rig.baseline_reader().statements
    embedding_read_index = next(i for i, q in enumerate(statements) if "embedding IS NULL" in q)
    serialize_start_index = next(i for i, q in enumerate(statements) if q == "CALL db.labels()")
    assert embedding_read_index < serialize_start_index
    # The embedding went through the gateway as one logged group before serialization began.
    assert len(rig.baseline_groups()) == 1
    # The embedding was computed before the group that carried it was submitted.
    assert embedding_caller.calls == ["Data Encryption"]


def test_export_instrument_returns_the_manifest_matching_the_written_file(
    tmp_path: Path, make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    manifest = _export(
        rig,
        _CRA_DESCRIPTOR,
        repo_root,
        packaged_copy_path,
        _FakeEmbeddingCaller([0.1, 0.2]),
        emitter,
    )

    instrument_dir = repo_root / "curated-content" / "CRA-1.0"
    document = json.loads((instrument_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == InstrumentManifest(**document)


def test_export_instrument_never_serializes_a_resolution_marker_from_the_single_tenant_graph(
    tmp_path: Path, make_emitter: MakeEmitter, rig: ExportRig
) -> None:
    """Only the instrument's own baseline and native graphs are serialized.

    A `ResolvedCapability` marker lives only in `policy_system`, so no artifact can carry it.
    """
    emitter, _log_path = make_emitter()
    repo_root = tmp_path / "repo"
    policy_system = rig.rig.graphs.open("policy_system")
    rig.seed_node(policy_system, "ResolvedCapability", "cap_x", {"resolves_to": "cap_1"})

    _export(
        rig,
        _CRA_DESCRIPTOR,
        repo_root,
        tmp_path / "packaged" / "catalog.json",
        _FakeEmbeddingCaller([0.1, 0.2]),
        emitter,
    )

    instrument_dir = repo_root / "curated-content" / "CRA-1.0"
    baseline_text = (instrument_dir / "baseline.json").read_text(encoding="utf-8")
    native_text = (instrument_dir / "native.json").read_text(encoding="utf-8")
    assert "ResolvedCapability" not in baseline_text
    assert "ResolvedCapability" not in native_text
    exported_ids = [node["properties"]["id"] for node in json.loads(baseline_text)["nodes"]]
    assert "cap_1" in exported_ids
    assert ("ResolvedCapability", "cap_x") in policy_system.nodes
