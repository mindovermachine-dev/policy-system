"""Live proof for PLAN.md Slice 3.6 (D15): the real, permanently-seeded
`engprac_baseline`/`engprac_native` FalkorDB graph pair (seeded once by
`tools/curated-export/migrate_engineering_practices.py` from
`test-data/engineering-practices/engineering-practices-seed.json`) is a
shape-correct baseline graph, and Slice 3.5's `export_instrument`
orchestration runs against it end to end.

Two things this test deliberately does NOT do:

- It does not create-then-delete `engprac_baseline`/`engprac_native` --
  unlike this suite's other `falkordb_live` tests' uniquely-tokened
  throwaway keys, these are meant to be the real, permanent curated-content
  source graphs (D15's whole point), so this test only ever reads them (plus
  the one documented D7 side effect below) and never deletes them.
- It does not use a live LLM provider (no `llm_live` marker, and this
  sandbox has no configured provider credentials) -- `export_instrument` is
  called with a fake, deterministic `EmbeddingCaller`, exactly like
  `CHANGES.md` MA5's own synthetic round-trip test. This still exercises
  every other part of the real export pipeline (real FalkorDB reads, real
  `SET n.embedding = ...` writes, real serialize/checksum/manifest/catalog
  logic) against genuinely-shaped internal-source content -- a
  fakes-in-the-embedding-seam proof, not a fakes-only proof.

Running this test writes a real `embedding` property onto every Capability/
Policy node in the live `engprac_baseline` graph that doesn't already carry
one (D7's documented side effect of running Export) -- idempotent on rerun,
since `backfill_capability_embeddings` only ever targets nodes with
`embedding IS NULL`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.company_merge.falkordb_client import select_graph as select_company_merge_graph
from ps_service.company_merge.graph_reader import read_baseline_graph
from ps_service.export.export_instrument import InstrumentDescriptor, export_instrument
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.serialize import parse_serialized_graph_json

if TYPE_CHECKING:
    from pathlib import Path

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB

_BASELINE_GRAPH_NAME = "engprac_baseline"
_NATIVE_GRAPH_NAME = "engprac_native"
_REGULATORY_INSTRUMENT_ID = "ENGPRAC-3.0"


class _QueryResult(Protocol):
    """Structural stand-in for `falkordb.QueryResult` -- the one field this test reads."""

    @property
    def result_set(self) -> list[object]: ...


def _count(result: object) -> int:
    """Read a `MATCH (...) RETURN count(...)` query result's scalar count.

    `GraphQueryResult.result_set`/the raw `object` a `_GraphQueryHandle.query()`
    call returns are both typed too loosely for basedpyright to index directly
    -- this is the one cast site every count read in this test goes through.
    """
    rows = cast("list[list[object]]", cast("_QueryResult", result).result_set)
    return cast("int", rows[0][0])


class _FakeEmbeddingCaller:
    """A deterministic embedding stand-in -- no `llm_live` marker, no real provider call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        assert len(inputs) == 1
        text = inputs[0]
        self.calls.append(text)
        vector = [float(len(text) % 7), 0.5]
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


@pytest.mark.falkordb_live
def test_engprac_baseline_passes_company_merges_own_baseline_graph_shape_assertions(
    live_falkordb: FalkorDB,
) -> None:
    """`read_baseline_graph` -- Company Merge's own baseline-graph reader --
    succeeds against `engprac_baseline` and returns non-empty node
    collections all the way through Policy/Standard/Control (D15: "a
    baseline graph that genuinely continues through Policy/Standard/
    Control, not just Capability").
    """
    baseline_graph = select_company_merge_graph(live_falkordb, _BASELINE_GRAPH_NAME)

    result = read_baseline_graph(baseline_graph, _REGULATORY_INSTRUMENT_ID)

    assert result.regulatory_instrument_properties["id"] == _REGULATORY_INSTRUMENT_ID
    assert result.regulatory_instrument_properties["source_type"] == "internal"
    assert len(result.role_nodes) > 0
    assert len(result.requirement_nodes) > 0
    assert len(result.obligation_nodes) > 0
    assert len(result.capability_nodes) > 0
    # `read_baseline_graph` reads only through Capability (Company Merge's own scope,
    # PLAN.md §0.3) -- Policy/Standard/Control depth is proven directly below via a
    # generic query, not through this reader.
    assert _count(baseline_graph.query("MATCH (n:Policy) RETURN count(n)")) > 0
    assert _count(baseline_graph.query("MATCH (n:Standard) RETURN count(n)")) > 0
    assert _count(baseline_graph.query("MATCH (n:Control) RETURN count(n)")) > 0


@pytest.mark.falkordb_live
def test_export_instrument_runs_end_to_end_against_the_real_engprac_graphs(
    live_falkordb: FalkorDB, tmp_path: Path, make_emitter: MakeEmitter
) -> None:
    """The real `export_instrument()` orchestration (Slice 3.5), run against
    the real `engprac_baseline`/`engprac_native` graphs -- a real-content
    proof of the whole export pipeline, not just a fakes-only proof (module
    docstring). Writes into a `tmp_path` repo root, never the real
    `curated-content/` tree (this test must stay repeatable and must not
    give every rerun a fresh `exported_at` timestamp diff in the real repo).
    """
    emitter, _log_path = make_emitter()
    embedding_caller = _FakeEmbeddingCaller()
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"
    descriptor = InstrumentDescriptor(
        short_name="ENGPRAC",
        instrument_id=_REGULATORY_INSTRUMENT_ID,
        version="3.0",
        celex=None,
        title="Engineering Practices Regulation",
        source_type="internal",
        jurisdiction=None,
    )

    manifest = export_instrument(
        descriptor,
        baseline_graph=graph_query_handle(live_falkordb, _BASELINE_GRAPH_NAME),
        native_graph=graph_query_handle(live_falkordb, _NATIVE_GRAPH_NAME),
        embed_model="fake-embed-model",
        repo_root=repo_root,
        packaged_copy_path=packaged_copy_path,
        call_embedding=embedding_caller,
        emitter=emitter,
    )

    assert manifest.instrument_id == _REGULATORY_INSTRUMENT_ID
    assert manifest.source_type == "internal"
    assert manifest.celex is None
    assert manifest.jurisdiction is None

    instrument_dir = repo_root / "curated-content" / _REGULATORY_INSTRUMENT_ID
    baseline_document = json.loads((instrument_dir / "baseline.json").read_text(encoding="utf-8"))
    labels_present = {node["label"] for node in baseline_document["nodes"]}
    assert {"Role", "Requirement", "Obligation", "Capability", "Policy", "Standard", "Control"} <= (
        labels_present
    )

    capability_nodes = [n for n in baseline_document["nodes"] if n["label"] == "Capability"]
    assert all("embedding" in node["properties"] for node in capability_nodes)
    policy_nodes = [n for n in baseline_document["nodes"] if n["label"] == "Policy"]
    assert all("embedding" in node["properties"] for node in policy_nodes)

    # Lossless-content proof: the serialized artifact's node/edge counts match a
    # direct count against the live source graphs (D1: dump what's there, plus D7's
    # embedding side effect only -- no node/edge silently dropped or duplicated).
    native_document = json.loads((instrument_dir / "native.json").read_text(encoding="utf-8"))
    parsed_baseline = parse_serialized_graph_json((instrument_dir / "baseline.json").read_bytes())
    parsed_native = parse_serialized_graph_json((instrument_dir / "native.json").read_bytes())
    baseline_graph_handle = graph_query_handle(live_falkordb, _BASELINE_GRAPH_NAME)
    live_node_count = _count(baseline_graph_handle.query("MATCH (n) RETURN count(n)"))
    live_edge_count = _count(baseline_graph_handle.query("MATCH ()-[r]->() RETURN count(r)"))
    assert len(parsed_baseline.nodes) == live_node_count
    assert len(parsed_baseline.edges) == live_edge_count
    assert len(parsed_native.nodes) == len(native_document["nodes"]) == 1
