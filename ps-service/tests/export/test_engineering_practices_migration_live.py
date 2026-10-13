"""Live proof for PLAN.md Slice 3.6 (D15): the engineering-practices `{short}_baseline`/
`{short}_native` graph pair, built by the real internal-seed adapter from the shipped
`internal-sources/engineering-practices/engineering-practices-seed.json`, is a shape-correct
baseline graph, and Slice 3.5's `export_instrument` orchestration runs against it end to end.

The pair is seeded by the `engprac_graphs` fixture into uniquely named graphs (deleted
afterwards) through the real Graph Write Gateway on a scratch PS Postgres. The test used to
read permanently seeded `engprac_baseline`/`engprac_native` graphs that only a hand-seeded dev
FalkorDB had, so it failed in any clean environment; it now owns its data and never reads or
writes those shared keys.

It does not use a live LLM provider (no `llm_live` marker): `export_instrument` is called with a
fake, deterministic `EmbeddingCaller`, exactly like `CHANGES.md` MA5's own synthetic round-trip
test. Every other part of the pipeline (real FalkorDB reads, real `SET n.embedding = ...`
writes, real serialize/checksum/manifest/catalog logic) runs against genuinely shaped
internal-source content.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast

import pytest
from litellm.types.utils import Embedding, EmbeddingResponse

from export.live_gateway import live_write_target
from ps_service.company_merge.graph_reader import read_baseline_graph
from ps_service.export.export_instrument import InstrumentDescriptor, export_instrument
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.export.serialize import parse_serialized_graph_json
from ps_service.graph_gateway.connection import select_graph as select_company_merge_graph
from ps_service.ingestion.adapters.internal_seed.adapter import InternalSeedIngestionAdapter
from ps_service.ingestion.adapters.internal_seed.persist import (
    ingest_internal_regulatory_instrument,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

_REGULATORY_INSTRUMENT_ID = "ENGPRAC-1.0"
_SEED_PATH = (
    Path(__file__).resolve().parents[3]
    / "internal-sources"
    / "engineering-practices"
    / "engineering-practices-seed.json"
)


class _EngpracGraphs(NamedTuple):
    """The names of the uniquely named baseline and native graphs the fixture seeded."""

    baseline: str
    native: str


@pytest.fixture
def engprac_graphs(
    live_falkordb: FalkorDB, provisioned_graph_log: Provisioned
) -> Iterator[_EngpracGraphs]:
    """Ingest the shipped engineering-practices seed into throwaway graphs; delete them after."""
    token = uuid.uuid4().hex[:12]
    names = _EngpracGraphs(baseline=f"engprac_{token}_baseline", native=f"engprac_{token}_native")
    seed = InternalSeedIngestionAdapter().parse_seed(json.loads(_SEED_PATH.read_text("utf-8")))
    try:
        with live_write_target(provisioned_graph_log, live_falkordb, names.baseline) as target:
            ingest_internal_regulatory_instrument(
                seed,
                submitter=target["submitter"],  # pyright: ignore[reportArgumentType]
                native_graph_name=names.native,
                baseline_graph_name=names.baseline,
                audit_event_id=cast("str", target["audit_event_id"]),
            )
        yield names
    finally:
        live_falkordb.connection.delete(names.baseline, names.native)


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
    live_falkordb: FalkorDB, engprac_graphs: _EngpracGraphs
) -> None:
    """`read_baseline_graph` -- Company Merge's own baseline-graph reader --
    succeeds against `engprac_baseline` and returns non-empty node
    collections all the way through Policy/Standard/Control (D15: "a
    baseline graph that genuinely continues through Policy/Standard/
    Control, not just Capability").
    """
    baseline_graph = select_company_merge_graph(live_falkordb, engprac_graphs.baseline)

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
@pytest.mark.postgres_live
def test_export_instrument_runs_end_to_end_against_the_real_engprac_graphs(
    live_falkordb: FalkorDB,
    provisioned_graph_log: Provisioned,
    engprac_graphs: _EngpracGraphs,
    tmp_path: Path,
    make_emitter: MakeEmitter,
) -> None:
    """The real `export_instrument()` orchestration (Slice 3.5), run against
    the seeded engineering-practices graphs -- a real-content
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
        version="1.0",
        celex=None,
        title="Engineering Practices Policy",
        source_type="internal",
        jurisdiction=None,
    )

    with live_write_target(provisioned_graph_log, live_falkordb, engprac_graphs.baseline) as target:
        manifest = export_instrument(
            descriptor,
            baseline_graph=graph_query_handle(live_falkordb, engprac_graphs.baseline),
            native_graph=graph_query_handle(live_falkordb, engprac_graphs.native),
            embed_model="fake-embed-model",
            repo_root=repo_root,
            packaged_copy_path=packaged_copy_path,
            call_embedding=embedding_caller,
            emitter=emitter,
            **target,  # pyright: ignore[reportArgumentType]
        )

    assert manifest.instrument_id == _REGULATORY_INSTRUMENT_ID
    assert manifest.source_type == "internal"
    assert manifest.celex is None
    assert manifest.jurisdiction is None

    instrument_dir = repo_root / "curated-content" / _REGULATORY_INSTRUMENT_ID
    baseline_document = json.loads((instrument_dir / "baseline.json").read_text(encoding="utf-8"))
    labels_present = {node["label"] for node in baseline_document["nodes"]}
    assert {
        "Role",
        "Requirement",
        "Obligation",
        "Capability",
        "Policy",
        "Standard",
        "Control",
        "PracticeArea",
        "RiskPath",
    } <= labels_present

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
    baseline_graph_handle = graph_query_handle(live_falkordb, engprac_graphs.baseline)
    native_graph_handle = graph_query_handle(live_falkordb, engprac_graphs.native)
    live_node_count = _count(baseline_graph_handle.query("MATCH (n) RETURN count(n)"))
    live_edge_count = _count(baseline_graph_handle.query("MATCH ()-[r]->() RETURN count(r)"))
    live_native_node_count = _count(native_graph_handle.query("MATCH (n) RETURN count(n)"))
    assert len(parsed_baseline.nodes) == live_node_count
    assert len(parsed_baseline.edges) == live_edge_count
    assert len(parsed_native.nodes) == len(native_document["nodes"]) == live_native_node_count
