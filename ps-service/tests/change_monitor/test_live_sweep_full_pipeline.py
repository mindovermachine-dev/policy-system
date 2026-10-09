"""Live FalkorDB proof of the full-pipeline amendment re-ingest (issue #201, AC-BI-002/004/008).

Entry is the REAL `run_change_check_sweep`; everything below it is real: `read_tracked_instruments`,
`will_reingest` / `trigger_reingestion` / `classify_reingestion`, the succession Cypher, the shared
stage sequence and the REAL Ingestion, Domain Mapper (extract, derive) and Company Merge stages with
the REAL Cellar/ELI Domain Mapping Adapter, over three disposable FalkorDB graphs
(`live201_native`, `live201_baseline` and a single-tenant graph named by `PS_FALKORDB_GRAPH`).
`policy_system` itself is never touched.

Stubbed boundaries (all on `approved-mock-boundaries.yaml`): the Cellar fetch (a scripted
`IngestionAdapter` returning a v1 structure with one Article, then the amendment structure with
that Article plus a new one), the poll (a scripted `PollReport`; Cellar again), the audit store, and
the LLM (`litellm.completion` / `litellm.embedding`, answering deterministically from the prompt's
own text). The LLM stub counts its calls so "a settled sweep runs no stage" is observable.

What the real run proves that the hermetic `test_reingestion_graph_state.py` cannot:

- version scoping: extract and derive read ONLY the new version's Articles and Requirements
  (the new version's `EXPRESSES` edges exist in the real baseline and the derive query follows
  them), so the prior version's subtree is untouched in all three graphs (AC-BI-004);
- the new Obligation and Capability exist in the single-tenant graph and the audited counts equal
  the graph delta (AC-BI-002, AC-BI-011);
- a derive failure followed by a retry re-runs only the missing stages and ends exactly like a clean
  run, with no duplicate node or edge (AC-BI-008);
- the succession statements run on real FalkorDB: edge with `absorbed`, prior `superseded` in the
  native and single-tenant graphs, no marker left behind.

Run explicitly (needs a reachable FalkorDB; set `PS_FALKORDB_HOST` / `PS_FALKORDB_PORT` if it is not
on the default):

    uv run pytest ps-service/tests/change_monitor/test_live_sweep_full_pipeline.py \
        -m "falkordb_live and not postgres_live" -n0 -q
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date
from typing import TYPE_CHECKING, cast

import pytest
from api._audit_fakes import InMemoryAuditStore
from litellm.types.utils import Choices, Embedding, EmbeddingResponse, Message, ModelResponse

from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import (
    ChangeCheckResult,
    build_default_change_check_dependencies,
    run_change_check_sweep,
)
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    build_default_pipeline_dependencies,
)
from ps_service.audit import AuditContext
from ps_service.change_monitor.falkordb_client import (
    GraphHandle,
    check_connectivity,
    connect_from_config,
    native_graph_name,
    select_graph,
)
from ps_service.change_monitor.models import AmendmentFinding, PollReport, TrackedInstrumentNode
from ps_service.config import ServiceConfig, load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.domain_mapper.prompts import (
    CAPABILITY_DERIVATION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    OBLIGATION_DERIVATION_SYSTEM_PROMPT,
)
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)
from ps_service.logging import facade

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from falkordb import FalkorDB

    from change_monitor._fakes import MakeEmitter


pytestmark = pytest.mark.falkordb_live

_SHORT = "LIVE201"
_CELEX = "32099R0201"
_NEW_VERSION = "02099R0201-20990101"
_PRIOR = f"{_SHORT}-1.0"
_NEW = f"{_SHORT}-{_NEW_VERSION}"
_SINGLE_TENANT = "policy_system_live201_test"
_ALL_STAGES = ("ingestion", "extraction", "derivation", "merge")

_ART_1 = "The manufacturer shall keep the technical documentation up to date."
_ART_2 = "The manufacturer shall notify actively exploited vulnerabilities to the authority."
_ROLE = "Manufacturer"

# Every node and edge type of the spine, so a snapshot sees all of it.
_NODES_QUERY = "MATCH (n) RETURN labels(n)[0], n.id, properties(n)"
_EDGES_QUERY = "MATCH (a)-[r]->(b) RETURN type(r), a.id, b.id, properties(r)"

_CONFIG_BASE = load_config()


class _ScriptedAdapter:
    """The Cellar boundary: serves the structure of whichever version `rid` currently names."""

    def __init__(self) -> None:
        self.rid = _PRIOR
        self.articles: tuple[str, ...] = (_ART_1,)
        self.calls = 0

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        assert identifier == _CELEX
        self.calls += 1
        nodes = tuple(
            StructuralNode(
                "ARTICLE",
                f"{self.rid}#art_{index}",
                {
                    "text": text,
                    "citation_ref": f"Art. {index}",
                    "heading": "Obligations of manufacturers",
                    "order": index,
                },
            )
            for index, text in enumerate(self.articles, start=1)
        )
        edges = tuple(
            StructuralEdge("RegulatoryInstrument", self.rid, "ARTICLE", node.id) for node in nodes
        )
        return FetchedRegulatoryInstrumentStructure(
            metadata=RegulatoryInstrumentMetadata(
                title="Live 201 Regulation",
                jurisdiction="EU",
                effective_date=date(2099, 1, 1),
                version=self.rid.removeprefix(f"{_SHORT}-"),
                status="active",
                source_type="external",
                instrument_type="regulation",
                celex=_CELEX,
            ),
            nodes=nodes,
            edges=edges,
        )

    def fetch_regulatory_instrument_metadata(self, identifier: str) -> RegulatoryInstrumentMetadata:
        return self.fetch_regulatory_instrument_structure(identifier).metadata


class _StubLlm:
    """`litellm.completion` / `litellm.embedding`, answering from the prompt's own text.

    Extraction returns one requirement (the Article text under the role `Manufacturer`),
    obligation derivation mints the requirement text as the Obligation, capability derivation mints
    `Capability for <obligation text>`. Embeddings give every distinct text its own axis, so two
    different Capability names never match semantically and an identical name always does.
    `fail_derivation_once` raises on the first obligation-derivation call (a partial derive).
    """

    def __init__(self) -> None:
        self.completions = 0
        self.embeddings = 0
        self.fail_derivation_once = False
        self._axes: dict[str, int] = {}

    def completion(self, **kwargs: object) -> ModelResponse:
        self.completions += 1
        messages = cast("list[dict[str, str]]", kwargs["messages"])
        system, user = messages[0]["content"], messages[1]["content"]
        if system == EXTRACTION_SYSTEM_PROMPT:
            text = user.split("<regulation_text>\n", 1)[1].split("\n</regulation_text>", 1)[0]
            body: dict[str, object] = {
                "requirements": [
                    {
                        "role_name": _ROLE,
                        "text": text,
                        "type": "requirement",
                        "letter_suffix": None,
                        "confidence": 0.9,
                    }
                ]
            }
        elif system == OBLIGATION_DERIVATION_SYSTEM_PROMPT:
            if self.fail_derivation_once:
                self.fail_derivation_once = False
                raise RuntimeError("stubbed provider outage")
            text = user.split("<requirement_text>\n", 1)[1].split("\n</requirement_text>", 1)[0]
            body = {
                "matched_existing_id": None,
                "new_text": text,
                "unmatchable": False,
                "confidence": 0.9,
            }
        elif system == CAPABILITY_DERIVATION_SYSTEM_PROMPT:
            text = user.split("<obligation_text>\n", 1)[1].split("\n</obligation_text>", 1)[0]
            body = {
                "capabilities": [
                    {
                        "matched_existing_id": None,
                        "new_name": f"Capability for {text}",
                        "new_description": None,
                        "confidence": 0.85,
                    }
                ]
            }
        else:  # pragma: no cover -- an unscripted prompt is a test-authoring bug
            raise AssertionError(f"unscripted LLM prompt: {system[:60]!r}")
        return ModelResponse(
            id="x",
            model="stub",
            choices=[
                Choices(
                    finish_reason="stop",
                    index=0,
                    message=Message(content=json.dumps(body), role="assistant"),
                )
            ],
        )

    def embedding(self, **kwargs: object) -> EmbeddingResponse:
        self.embeddings += 1
        [text] = cast("list[str]", kwargs["input"])
        axis = self._axes.setdefault(text, len(self._axes))
        vector = [0.0] * 32
        vector[axis] = 1.0
        return EmbeddingResponse(
            model="stub", data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


Snapshot = tuple[dict[tuple[str, str], dict[str, object]], dict[tuple[str, str, str], object]]


def _snapshot(graph: GraphHandle) -> Snapshot:
    """Every node `(label, id) -> properties` and edge `(type, src, dst) -> properties`."""
    nodes: dict[tuple[str, str], dict[str, object]] = {}
    for label, node_id, props in cast("list[list[object]]", graph.query(_NODES_QUERY).result_set):
        nodes[(cast("str", label), cast("str", node_id))] = cast("dict[str, object]", props)
    edges: dict[tuple[str, str, str], object] = {}
    for rel, src, dst, props in cast("list[list[object]]", graph.query(_EDGES_QUERY).result_set):
        edges[(cast("str", rel), cast("str", src), cast("str", dst))] = props
    return nodes, edges


def _stable(snapshot: Snapshot) -> Snapshot:
    """A snapshot with the run-specific values removed, for comparing two separate runs.

    `created_at` is a wall-clock stamp and a `PendingReview`'s id is a fresh uuid per run (it is
    re-keyed by the Capability it concerns); everything else must be identical.
    """
    review_key = {
        node_id: cast("str", props["incoming_id"])
        for (label, node_id), props in snapshot[0].items()
        if label == "PendingReview"
    }
    nodes = {
        (label, review_key.get(node_id, node_id)): {
            k: v for k, v in props.items() if k not in ("created_at", "id")
        }
        for (label, node_id), props in snapshot[0].items()
    }
    edges = {
        (rel, review_key.get(src, src), review_key.get(dst, dst)): props
        for (rel, src, dst), props in snapshot[1].items()
    }
    return nodes, edges


def _count(graph: GraphHandle, query: str, params: dict[str, object] | None = None) -> int:
    rows = cast("list[list[object]]", graph.query(query, params=params).result_set)
    return cast("int", rows[0][0])


def _ids(graph: GraphHandle, label: str) -> set[str]:
    rows = cast("list[list[object]]", graph.query(f"MATCH (n:{label}) RETURN n.id").result_set)
    return {cast("str", row[0]) for row in rows}


@dataclasses.dataclass
class _World:
    """The three disposable graphs plus the boundary doubles for one test."""

    native: GraphHandle
    baseline: GraphHandle
    single_tenant: GraphHandle
    adapter: _ScriptedAdapter
    llm: _StubLlm
    config: ServiceConfig
    pipeline: PipelineDependencies
    db: FalkorDB
    names: tuple[str, ...]

    def graphs(self) -> tuple[GraphHandle, GraphHandle, GraphHandle]:
        return self.native, self.baseline, self.single_tenant

    def snapshots(self) -> tuple[Snapshot, Snapshot, Snapshot]:
        return _snapshot(self.native), _snapshot(self.baseline), _snapshot(self.single_tenant)

    def sweep(
        self,
        emitter: object,
        store: InMemoryAuditStore,
        *,
        force_tracked: tuple[TrackedInstrumentNode, ...] | None = None,
        amend: bool = True,
    ) -> ChangeCheckResult:
        """One REAL sweep. `force_tracked` overrides only the tracked read (a lagging poll)."""
        real = build_default_change_check_dependencies()
        prior_read = real.read_tracked_instruments

        def _tracked(graph: GraphHandle) -> tuple[TrackedInstrumentNode, ...]:
            return force_tracked if force_tracked is not None else prior_read(graph)

        def _poll(graph: GraphHandle, *, emitter: object = None) -> PollReport:
            tracked = _tracked(graph)
            findings = tuple(
                AmendmentFinding(
                    regulatory_instrument_id=node.regulatory_instrument_id,
                    instrument_type=node.instrument_type,
                    baseline_reference=node.effective_date,
                    detected_consolidated_celex=_NEW_VERSION,
                    detected_consolidation_date=date(2099, 6, 1),
                    reason="newer_consolidation",
                )
                for node in tracked
                if amend and node.regulatory_instrument_id == _PRIOR
            )
            return PollReport(
                findings=findings, polled_count=len(tracked), failed_ids=(), unconfigured_ids=()
            )

        entry = CatalogEntry(celex=_CELEX, title="Live 201", short_name=_SHORT, version="1.0")

        def _find_entry(celex: str) -> CatalogEntry | None:
            return entry if celex == _CELEX else None

        deps = dataclasses.replace(
            real,
            pipeline=self.pipeline,
            read_tracked_instruments=_tracked,
            poll_for_amendments=_poll,  # pyright: ignore[reportArgumentType]
            default_adapter=lambda: self.adapter,
            find_catalog_entry=_find_entry,
        )
        return run_change_check_sweep(
            config=self.config,
            run_id="live-sweep",
            dependencies=deps,
            audit=AuditContext(("caller", "https://issuer.example.com/"), store),
            emitter=emitter,  # pyright: ignore[reportArgumentType]
        )


def _drop(db: FalkorDB, names: tuple[str, ...]) -> None:
    """Delete each named graph that exists (best effort pre-clean and teardown)."""
    present = set(db.list_graphs())
    for name in names:
        if name in present:
            db.select_graph(name).delete()


def _build_world(monkeypatch: pytest.MonkeyPatch, emitter: object) -> Iterator[_World]:
    monkeypatch.setenv("PS_FALKORDB_GRAPH", _SINGLE_TENANT)
    config = dataclasses.replace(
        _CONFIG_BASE,
        llm_interface_model="azure/stub-chat",
        llm_interface_embed_model="azure/stub-embed",
        company_merge_similarity_threshold=0.83,
    )
    db = connect_from_config(config)
    check_connectivity(db, host=config.falkordb_host, port=config.falkordb_port)
    names = (native_graph_name(_SHORT), baseline_graph_name(_SHORT), _SINGLE_TENANT)
    _drop(db, names)
    llm = _StubLlm()
    monkeypatch.setattr("ps_service.llm_interface.client.litellm.completion", llm.completion)
    monkeypatch.setattr("ps_service.llm_interface.client.litellm.embedding", llm.embedding)
    adapter = _ScriptedAdapter()
    try:
        world = _World(
            native=select_graph(db, names[0]),
            baseline=select_graph(db, names[1]),
            single_tenant=select_graph(db, names[2]),
            adapter=adapter,
            llm=llm,
            config=config,
            pipeline=build_default_pipeline_dependencies(),
            db=db,
            names=names,
        )
        # Seed v1 through the real catalog stage sequence (ingest, extract, derive, merge).
        from ps_service.api.ingestion_orchestration import (
            run_catalog_ingestion_pipeline,
        )

        outcome = run_catalog_ingestion_pipeline(
            CatalogEntry(celex=_CELEX, title="Live 201", short_name=_SHORT, version="1.0"),
            config=config,
            run_id="live-seed",
            caller="test",
            dependencies=world.pipeline,
            emitter=emitter,  # pyright: ignore[reportArgumentType]
            ingestion_adapter=adapter,
        )
        assert outcome.outcome != "already_ingested"
        adapter.rid = _NEW
        adapter.articles = (_ART_1, _ART_2)
        yield world
    finally:
        _drop(db, names)


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch, make_emitter: MakeEmitter, tmp_path: Path
) -> Iterator[_World]:
    """The disposable graphs, v1 seeded, with a real process-wide log facade installed.

    The default pipeline stages emit through the process-default emitter (they take none), so a
    real `configure()`d facade is installed for the test and restored afterwards, including the
    `atexit` guard that `reset_for_tests()` deliberately leaves set.
    """
    emitter, _ = make_emitter()
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]
    facade.configure(log_path=tmp_path / "facade.jsonl")
    try:
        yield from _build_world(monkeypatch, emitter)
    finally:
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


def test_seeded_prior_version_is_a_complete_single_version_spine(world: _World) -> None:
    """Sanity of the fixture: v1 is mapped and merged, and only v1."""
    assert _ids(world.single_tenant, "RegulatoryInstrument") == {_PRIOR}
    assert _count(world.single_tenant, "MATCH (n:Obligation) RETURN count(n)") == 1
    assert _count(world.single_tenant, "MATCH (n:Capability) RETURN count(n)") == 1
    assert (
        _count(world.baseline, "MATCH (:RegulatoryInstrument)-[:EXPRESSES]->(r) RETURN count(r)")
        == 1
    )


def test_amendment_runs_the_real_stages_and_absorbs_without_touching_the_prior_version(
    world: _World, make_emitter: MakeEmitter
) -> None:
    """AC-BI-002, AC-BI-003, AC-BI-004, AC-BI-011 against real FalkorDB.

    AC-BI-004 precise statement: the sweep's succession write intentionally flips the prior
    `RegulatoryInstrument.status` active -> superseded (native and single-tenant graphs) and adds
    the one `SUPERSEDED_BY` edge. Every other node, edge and property that existed before the sweep
    (all three graphs, including the baseline where the prior stays `active`) is equal afterwards.

    One more documented exception, found by this live run: a `Capability` is a canonical,
    company-level node (shared across versions and regulations), and Company Merge's existing
    dedup index computes and caches its `embedding` the first time a later merge compares a
    new Capability against it (`WHERE n.embedding IS NULL`; v1's merge had nothing to compare
    with). That is an additive cache key on a shared node written by the unchanged merge
    algorithm, not an edit of prior-version content; the test allows exactly that key and nothing
    else on the single-tenant graph's Capabilities.
    """
    emitter, _ = make_emitter(filename="sweep.jsonl")
    before = world.snapshots()
    obligations_before = _ids(world.single_tenant, "Obligation")
    capabilities_before = _ids(world.single_tenant, "Capability")
    store = InMemoryAuditStore()

    result = world.sweep(emitter, store)

    [outcome] = result.instruments
    assert (outcome.outcome, outcome.detail) == (
        "amendment_reingested",
        f"{_NEW} (superseded, fresh)",
    )
    # Succession statements (real Cypher): edge + absorbed, prior superseded, no marker left.
    native_after, baseline_after, tenant_after = world.snapshots()
    assert native_after[1][("SUPERSEDED_BY", _PRIOR, _NEW)] == {"absorbed": True}
    assert native_after[0][("RegulatoryInstrument", _PRIOR)]["status"] == "superseded"
    assert tenant_after[0][("RegulatoryInstrument", _PRIOR)]["status"] == "superseded"
    assert tenant_after[0][("RegulatoryInstrument", _NEW)]["status"] == "active"
    assert _count(world.native, "MATCH (m:ReingestProgress) RETURN count(m)") == 0
    assert _count(world.single_tenant, "MATCH ()-[e:SUPERSEDED_BY]->() RETURN count(e)") == 1

    # AC-BI-002: the amendment's new Obligation and Capability exist in the single-tenant graph.
    new_obligations = _ids(world.single_tenant, "Obligation") - obligations_before
    new_capabilities = _ids(world.single_tenant, "Capability") - capabilities_before
    obligation_texts = {
        cast("str", row[0])
        for row in cast(
            "list[list[object]]",
            world.single_tenant.query("MATCH (o:Obligation) RETURN o.text").result_set,
        )
    }
    assert _ART_2 in obligation_texts
    assert len(new_capabilities) == 1
    assert (
        _count(
            world.single_tenant,
            "MATCH (c:Capability) WHERE c.name = $n RETURN count(c)",
            {"n": f"Capability for {_ART_2}"},
        )
        == 1
    )
    # EXPRESSES edges are what the derive query follows: both versions have their own, and the
    # new version's derive saw exactly its two Requirements (no prior-version leakage).
    for rid, expected in ((_PRIOR, 1), (_NEW, 2)):
        assert (
            _count(
                world.baseline,
                "MATCH (:RegulatoryInstrument {id: $rid})-[:EXPRESSES]->(r:Requirement) "
                "RETURN count(r)",
                {"rid": rid},
            )
            == expected
        )
    # AC-BI-011: the audited counts are the graph delta; matched_capabilities is inclusive of the
    # prior version's Capability that the new version's Article 1 converges onto.
    complete = store.rows[1].details
    assert complete["new_obligations"] == len(new_obligations)
    assert complete["new_capabilities"] == len(new_capabilities) == 1
    assert cast("int", complete["matched_capabilities"]) >= 1

    # AC-BI-004: nothing the prior version owns changed, in any of the three graphs.
    flipped = ("RegulatoryInstrument", _PRIOR)
    for index, (was, now) in enumerate(zip(before, world.snapshots(), strict=True)):
        for key, props in was[0].items():
            expected_props = (
                {**props, "status": "superseded"} if key == flipped and index != 1 else props
            )
            actual_props = now[0][key]
            if key[0] == "Capability" and index == 2 and "embedding" not in props:
                # The one tolerated difference (see the docstring): Company Merge's dedup index
                # caches the embedding of an existing canonical Capability the first time a later
                # merge compares against it. Only that key may appear; nothing else may change.
                assert isinstance(actual_props.get("embedding"), list), key
                actual_props = {k: v for k, v in actual_props.items() if k != "embedding"}
            assert actual_props == expected_props, (index, key)
        for key, props in was[1].items():
            assert now[1][key] == props, (index, key)
    assert baseline_after[0][flipped]["status"] == "active"
    # Four stages ran for the new version (ingest 1 fetch; 2 extraction + 2 obligation + 2
    # capability completions; 2 capability embeddings or more).
    assert world.llm.completions >= 6
    assert world.adapter.calls >= 1
    assert [row.action for row in store.rows] == ["ingestion_run.submit", "ingestion_run.complete"]


def test_second_sweep_polls_the_new_version_current_and_a_lagging_poll_runs_nothing(
    world: _World, make_emitter: MakeEmitter
) -> None:
    """After absorption the prior leaves the tracked set; re-flagging it is `already_processed`."""
    emitter, _ = make_emitter(filename="sweep2.jsonl")
    world.sweep(emitter, InMemoryAuditStore())
    settled = world.snapshots()
    completions, embeddings = world.llm.completions, world.llm.embeddings

    second_store = InMemoryAuditStore()
    second = world.sweep(emitter, second_store, amend=False)

    assert [(o.instrument_id, o.outcome) for o in second.instruments] == [(_NEW, "current")]
    assert second_store.rows == []

    prior_node = TrackedInstrumentNode(
        regulatory_instrument_id=_PRIOR,
        celex=_CELEX,
        instrument_type="regulation",
        effective_date="2099-01-01",
    )
    third_store = InMemoryAuditStore()
    third = world.sweep(emitter, third_store, force_tracked=(prior_node,))

    [outcome] = third.instruments
    assert (outcome.outcome, outcome.detail) == (
        "amendment_reingested",
        f"{_NEW} (already_processed)",
    )
    assert third_store.rows == []
    assert (world.llm.completions, world.llm.embeddings) == (completions, embeddings)
    for got, want in zip(world.snapshots(), settled, strict=True):
        assert got == want


def test_derive_failure_then_retry_reruns_only_missing_stages_and_creates_no_duplicates(
    world: _World, make_emitter: MakeEmitter
) -> None:
    """AC-BI-007 / AC-BI-008 on real graphs: the retried run ends exactly like a clean one."""
    emitter, _ = make_emitter(filename="retry.jsonl")
    world.llm.fail_derivation_once = True

    first = world.sweep(emitter, InMemoryAuditStore())

    [failed] = first.instruments
    assert failed.outcome == "reingest_failed"
    assert failed.detail is not None
    assert failed.detail.startswith("pipeline_stage_failed")
    assert _count(world.native, "MATCH ()-[e:SUPERSEDED_BY]->() RETURN count(e)") == 0
    assert (
        _count(
            world.single_tenant,
            "MATCH (r:RegulatoryInstrument {id: $id}) RETURN count(r)",
            {"id": _PRIOR},
        )
        == 1
    )
    assert world.single_tenant.query(
        "MATCH (r:RegulatoryInstrument {id: $id}) RETURN r.status", params={"id": _PRIOR}
    ).result_set == [["active"]]
    marker = world.native.query("MATCH (m:ReingestProgress) RETURN m.id, m.stage").result_set
    assert marker == [[_NEW, "extraction"]]
    fetches_after_failure = world.adapter.calls

    second = world.sweep(emitter, InMemoryAuditStore())

    assert [o.outcome for o in second.instruments] == ["amendment_reingested"]
    assert world.adapter.calls == fetches_after_failure  # ingestion was not re-run
    retried = world.snapshots()

    # A clean run on emptied graphs must end with exactly the same nodes, edges and properties.
    _drop(world.db, world.names)
    clean = _clean_run_snapshot(world, emitter)
    for got, want in zip(retried, clean, strict=True):
        assert _stable(got) == _stable(want)


def _clean_run_snapshot(world: _World, emitter: object) -> tuple[Snapshot, Snapshot, Snapshot]:
    """Re-seed v1 into the (emptied) graphs and run one sweep with no failure."""
    from ps_service.api.ingestion_orchestration import (
        run_catalog_ingestion_pipeline,
    )

    world.adapter.rid = _PRIOR
    world.adapter.articles = (_ART_1,)
    run_catalog_ingestion_pipeline(
        CatalogEntry(celex=_CELEX, title="Live 201", short_name=_SHORT, version="1.0"),
        config=world.config,
        run_id="live-reseed",
        caller="test",
        dependencies=world.pipeline,
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        ingestion_adapter=world.adapter,
    )
    world.adapter.rid = _NEW
    world.adapter.articles = (_ART_1, _ART_2)
    world.sweep(emitter, InMemoryAuditStore())
    return world.snapshots()
