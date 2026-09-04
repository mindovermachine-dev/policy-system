"""Live FalkorDB proof of the real export -> artifact -> restore -> equality
chain (CHANGES.md MA5, CHANGES2.md §3.8 -- New Slice 5.10, appended to Batch
5). The first slice in the whole plan proving export and restore compose
correctly against real FalkorDB for a genuinely curated-instrument-shaped
graph.

1. Hand-author a real `curated_rt_baseline`/`curated_rt_native` FalkorDB
   graph pair directly via Cypher (2 Role, 2 Requirement, 3 Obligation, 3
   Capability nodes; the full `DEFINES`/`EXPRESSES`/`HAS`/`SATISFIED_BY`/
   `REQUIRES` edge set) -- the same fixture-construction convention
   `test_engineering_practices_migration_live.py` (Slice 3.6) already uses
   for `engprac_baseline`/`engprac_native`.
2. Call the REAL `export_instrument()` (Batch 3) against this pair, with a
   fake, deterministic `EmbeddingCaller` (no `llm_live` marker needed --
   embeddings' exact values are irrelevant to a lossless round-trip proof,
   only their presence/persistence is), writing into a `tmp_path` repo root
   plus a `tmp_path`-based `packaged_copy_path` (MA3's second parameter).
3. Read `curated-content/{instrument_id}/{manifest.json,baseline.json,
   native.json}` back off disk exactly as `ps-cli`'s future
   `catalog_repo.read_artifact` would (raw file reads -- Slice 7.1 doesn't
   exist yet) -- `catalog_writer.read_manifest` for the manifest fields
   (its own exact, already-real inverse of `write_manifest`), `Path.
   read_bytes()` directly for the two blobs.
4. Call the REAL `restore_instrument()` against a fresh, empty, dedicated
   single-tenant graph name (never `policy_system`) and the SAME
   `curated_rt_native`/`curated_rt_baseline` key names the source graphs
   used -- `RENAME`'s overwrite-destination semantics (D8 step 7) installs
   the round-tripped content over the originals, exactly like a real
   re-restore. The original native content is captured in memory (via
   `serialize_graph`) before export/restore ever runs, since restore
   overwrites the very same live keys.
5. Assert content equality: native is an exact structural match (no dedup,
   D20); every Role/Requirement/Obligation is present unchanged in the
   post-restore single-tenant graph; every Capability is present under its
   own id (all "new" mints, target starts empty) with `REQUIRES` edges
   correctly wired, each carrying the artifact-supplied embedding (D7).
6. Cleanup: delete every graph this test created, verified via `EXISTS`.
"""

from __future__ import annotations

import random
import uuid
from typing import TYPE_CHECKING

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.export import catalog_writer
from ps_service.export.export_instrument import InstrumentDescriptor, export_instrument
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.serialize import serialize_graph, to_json_bytes
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

_ACTOR = "test-actor-5-10"
_SIMILARITY_THRESHOLD = 0.9
_BASELINE_GRAPH_NAME = "curated_rt_baseline"
_NATIVE_GRAPH_NAME = "curated_rt_native"
_SHORT_NAME = "curated_rt"


class _FakeEmbeddingCaller:
    """A deterministic-per-text embedding stand-in -- no `llm_live` marker, no real provider call.

    This test's equality proof only cares that an embedding is present/
    persisted end to end, never its specific vector values -- but it must
    still distinguish genuinely different Capability names well enough not
    to falsely converge two of them (MA1's in-batch convergence fix would
    otherwise treat two unrelated Capabilities as a semantic match). Unlike
    `test_engineering_practices_migration_live.py`'s simpler
    `len(text) % 7`-based fake (fine there -- that test never compares two
    different Capability names against each other), this seeds an 8-dim
    vector from `text` itself (`random.Random(text)`) so unrelated names
    reliably score well below this test's 0.9 similarity threshold, while
    staying fully deterministic (repeated calls for the same text return
    the same vector, matching a real embedding model's own determinism).
    """

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        rng = random.Random(inputs[0])  # noqa: S311 -- deterministic test fixture, not security-sensitive
        vector = [rng.uniform(-1, 1) for _ in range(8)]
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def _seed_curated_graphs(live_falkordb: FalkorDB, instrument_id: str) -> None:
    """Hand-author `curated_rt_baseline`/`curated_rt_native` via real Cypher."""
    baseline = live_falkordb.select_graph(_BASELINE_GRAPH_NAME)
    baseline.query(
        "CREATE (:RegulatoryInstrument {id: $id, title: 'RT510 Regulation', "
        "source_type: 'external'})",
        {"id": instrument_id},
    )
    for role_id, name in (("RT510-role-1", "Role One"), ("RT510-role-2", "Role Two")):
        baseline.query(
            "CREATE (:Role {id: $id, name: $name, confidence: 0.9})", {"id": role_id, "name": name}
        )
    for req_id, role_id, text in (
        ("RT510-req-1", "RT510-role-1", "Requirement One"),
        ("RT510-req-2", "RT510-role-2", "Requirement Two"),
    ):
        baseline.query(
            "CREATE (:Requirement {id: $id, text: $text, type: 'obligation', "
            "confidence: 0.9, role_id: $role_id})",
            {"id": req_id, "text": text, "role_id": role_id},
        )
    for obl_id, text in (
        ("RT510-obl-1", "Obligation One"),
        ("RT510-obl-2", "Obligation Two"),
        ("RT510-obl-3", "Obligation Three"),
    ):
        baseline.query(
            "CREATE (:Obligation {id: $id, text: $text, confidence: 0.9})",
            {"id": obl_id, "text": text},
        )
    for cap_id, name in (
        ("RT510-cap-1", "Capability One"),
        ("RT510-cap-2", "Capability Two"),
        ("RT510-cap-3", "Capability Three"),
    ):
        baseline.query(
            "CREATE (:Capability {id: $id, name: $name, confidence: 0.9})",
            {"id": cap_id, "name": name},
        )
    for role_id in ("RT510-role-1", "RT510-role-2"):
        baseline.query(
            "MATCH (ri:RegulatoryInstrument {id: $id}), (r:Role {id: $role_id}) "
            "CREATE (ri)-[:DEFINES {source_ref: 'art. 1'}]->(r)",
            {"id": instrument_id, "role_id": role_id},
        )
    for req_id in ("RT510-req-1", "RT510-req-2"):
        baseline.query(
            "MATCH (ri:RegulatoryInstrument {id: $id}), (req:Requirement {id: $req_id}) "
            "CREATE (ri)-[:EXPRESSES {source_ref: 'art. 1'}]->(req)",
            {"id": instrument_id, "req_id": req_id},
        )
    for role_id, obl_id in (
        ("RT510-role-1", "RT510-obl-1"),
        ("RT510-role-2", "RT510-obl-2"),
        ("RT510-role-1", "RT510-obl-3"),
    ):
        baseline.query(
            "MATCH (r:Role {id: $role_id}), (o:Obligation {id: $obl_id}) CREATE (r)-[:HAS]->(o)",
            {"role_id": role_id, "obl_id": obl_id},
        )
    for req_id, obl_id in (
        ("RT510-req-1", "RT510-obl-1"),
        ("RT510-req-2", "RT510-obl-2"),
        ("RT510-req-1", "RT510-obl-3"),
    ):
        baseline.query(
            "MATCH (req:Requirement {id: $req_id}), (o:Obligation {id: $obl_id}) "
            "CREATE (req)-[:SATISFIED_BY]->(o)",
            {"req_id": req_id, "obl_id": obl_id},
        )
    for obl_id, cap_id in (
        ("RT510-obl-1", "RT510-cap-1"),
        ("RT510-obl-2", "RT510-cap-2"),
        ("RT510-obl-3", "RT510-cap-3"),
    ):
        baseline.query(
            "MATCH (o:Obligation {id: $obl_id}), (c:Capability {id: $cap_id}) "
            "CREATE (o)-[:REQUIRES]->(c)",
            {"obl_id": obl_id, "cap_id": cap_id},
        )

    native = live_falkordb.select_graph(_NATIVE_GRAPH_NAME)
    native.query(
        "CREATE (:RegulatoryInstrument {id: $id, title: 'RT510 Regulation'})", {"id": instrument_id}
    )
    native.query("CREATE (:TITLE {id: 'RT510-title-1', text: 'Title I'})")
    native.query("CREATE (:ARTICLE {id: 'RT510-art-1', text: 'Article 1 text'})")
    native.query(
        "MATCH (ri:RegulatoryInstrument {id: $id}), (t:TITLE {id: 'RT510-title-1'}) "
        "CREATE (ri)-[:HAS]->(t)",
        {"id": instrument_id},
    )
    native.query(
        "MATCH (t:TITLE {id: 'RT510-title-1'}), (a:ARTICLE {id: 'RT510-art-1'}) "
        "CREATE (t)-[:HAS]->(a)"
    )


@pytest.mark.falkordb_live
def test_export_then_restore_reproduces_native_exactly_and_baseline_correctly(
    live_falkordb: FalkorDB,
    tmp_path: Path,
    make_emitter: MakeEmitter,
    query_result_rows: Callable[[FalkorDB, str, str], list[list[object]]],
) -> None:
    emitter, _log_path = make_emitter()
    token = uuid.uuid4().hex[:12]
    instrument_id = f"RT510-{token}"
    single_tenant_graph_name = f"__ac66_slice510_single_tenant_{token}__"

    live_falkordb.connection.delete(_BASELINE_GRAPH_NAME, _NATIVE_GRAPH_NAME)
    _seed_curated_graphs(live_falkordb, instrument_id)

    # Captured BEFORE export/restore run, since restore's finalize step
    # RENAMEs its own restored content over these same live keys.
    original_native = serialize_graph(graph_query_handle(live_falkordb, _NATIVE_GRAPH_NAME))

    descriptor = InstrumentDescriptor(
        short_name=_SHORT_NAME,
        instrument_id=instrument_id,
        version="1.0",
        celex=None,
        title="RT510 Regulation",
        source_type="external",
        jurisdiction=None,
    )
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    try:
        export_instrument(
            descriptor,
            baseline_graph=graph_query_handle(live_falkordb, _BASELINE_GRAPH_NAME),
            native_graph=graph_query_handle(live_falkordb, _NATIVE_GRAPH_NAME),
            embed_model="fake-embed-model",
            repo_root=repo_root,
            packaged_copy_path=packaged_copy_path,
            call_embedding=_FakeEmbeddingCaller(),
            emitter=emitter,
        )

        # Step 3: read the artifact back off disk exactly as ps-cli's future
        # catalog_repo.read_artifact would -- raw file reads.
        instrument_dir = repo_root / "curated-content" / instrument_id
        manifest = catalog_writer.read_manifest(instrument_dir)
        baseline_blob = (instrument_dir / "baseline.json").read_bytes()
        native_blob = (instrument_dir / "native.json").read_bytes()
        artifact = RestoreArtifact(
            manifest=manifest, baseline_blob=baseline_blob, native_blob=native_blob
        )
        assert manifest.short_name == _SHORT_NAME

        # Step 4: restore against a fresh single-tenant graph; native/baseline
        # targets are the SAME curated_rt_native/curated_rt_baseline keys the
        # source graphs used -- RENAME overwrites them, a real re-restore.
        restore_instrument(
            artifact,
            db=live_falkordb,
            single_tenant_graph_name=single_tenant_graph_name,
            similarity_threshold=_SIMILARITY_THRESHOLD,
            actor=_ACTOR,
            emitter=emitter,
        )

        # Step 5a: native is an exact structural match -- no dedup (D20).
        restored_native = serialize_graph(graph_query_handle(live_falkordb, _NATIVE_GRAPH_NAME))
        assert to_json_bytes(restored_native) == to_json_bytes(original_native)

        # Step 5b: every Role/Requirement/Obligation is present, unchanged.
        role_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (n:Role) RETURN n.id, n.name ORDER BY n.id",
        )
        assert role_rows == [["RT510-role-1", "Role One"], ["RT510-role-2", "Role Two"]]

        requirement_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (n:Requirement) RETURN n.id, n.text ORDER BY n.id",
        )
        assert requirement_rows == [
            ["RT510-req-1", "Requirement One"],
            ["RT510-req-2", "Requirement Two"],
        ]

        obligation_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (n:Obligation) RETURN n.id, n.text ORDER BY n.id",
        )
        assert obligation_rows == [
            ["RT510-obl-1", "Obligation One"],
            ["RT510-obl-2", "Obligation Two"],
            ["RT510-obl-3", "Obligation Three"],
        ]

        # Step 5c: every Capability is present under its own id (all "new"
        # mints, target starts empty), each carrying the artifact-supplied
        # embedding (D7), with REQUIRES edges correctly wired.
        capability_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (n:Capability) RETURN n.id, n.name, n.embedding IS NOT NULL ORDER BY n.id",
        )
        assert capability_rows == [
            ["RT510-cap-1", "Capability One", True],
            ["RT510-cap-2", "Capability Two", True],
            ["RT510-cap-3", "Capability Three", True],
        ]

        requires_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) RETURN o.id, c.id ORDER BY o.id",
        )
        assert requires_rows == [
            ["RT510-obl-1", "RT510-cap-1"],
            ["RT510-obl-2", "RT510-cap-2"],
            ["RT510-obl-3", "RT510-cap-3"],
        ]

        has_rows = query_result_rows(
            live_falkordb,
            single_tenant_graph_name,
            "MATCH (r:Role)-[:HAS]->(o:Obligation) RETURN r.id, o.id ORDER BY o.id",
        )
        assert has_rows == [
            ["RT510-role-1", "RT510-obl-1"],
            ["RT510-role-2", "RT510-obl-2"],
            ["RT510-role-1", "RT510-obl-3"],
        ]
    finally:
        live_falkordb.connection.delete(
            _BASELINE_GRAPH_NAME, _NATIVE_GRAPH_NAME, single_tenant_graph_name
        )
        assert live_falkordb.connection.exists(_BASELINE_GRAPH_NAME) == 0
        assert live_falkordb.connection.exists(_NATIVE_GRAPH_NAME) == 0
        assert live_falkordb.connection.exists(single_tenant_graph_name) == 0
