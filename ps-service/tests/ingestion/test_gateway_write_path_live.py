"""`falkordb_live` + `postgres_live`: ingestion's gateway write path against real stores (#208).

The live twins of the fast equivalence, rerun, backfill, replay and embedding tests. The gateway
is the production wiring in miniature: the real `PsycopgGraphLogStore` as the `ps_state` role on a
scratch PS Postgres, and the real FalkorDB opener. Each test works on uniquely named graphs that
the `stack` fixture deletes afterwards.

What these prove that the fast tests (in-memory fakes) cannot:

* AC-BI-002, modulo identity (D2): the digest of the graph the migrated writers build equals the
  digest of the graph the frozen pre-#208 Cypher builds on the same FalkorDB, once the legacy
  edges are given the gateway's deterministic `identity` (the one stamp Cypher below).
* AC-BI-003 / R-embed: an embedding-less node stays embedding-free, and an existing embedding
  survives a re-ingest (`SET n.embedding = coalesce(row.embedding, n.embedding)`).
* AC-BI-005: a re-ingest changes nothing and logs nothing; a wiped graph is rebuilt from the log
  to the digest it had.
* AC-BI-004 (G1): an edge to a node that is nowhere is dropped silently, nothing logged for it.
* F1: re-ingesting onto a graph written before the gateway (edges without `identity`) adds no
  parallel edge.

Not runnable in the implementation sandbox (no FalkorDB module, no Postgres). To run: start a
FalkorDB and a scratch Postgres, then
`PS_TEST_FALKORDB_HOST=127.0.0.1 PS_TEST_FALKORDB_PORT=16379
PS_TEST_POSTGRES_SUPERUSER_DSN=postgresql://postgres@127.0.0.1:54329/postgres
uv run pytest ps-service/tests/ingestion/test_gateway_write_path_live.py
-m "falkordb_live and postgres_live"`.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_postgres import committed_audit_event

from ingestion.adapters.internal_seed._legacy_reference import write_legacy
from ingestion.adapters.internal_seed._seeds import full_seed
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.ingestion.adapters.internal_seed.adapter import InternalSeedIngestionAdapter
from ps_service.ingestion.adapters.internal_seed.persist import (
    ingest_internal_regulatory_instrument,
)
from ps_service.ingestion.falkordb_client import connect, native_graph_name, select_graph
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)
from ps_service.ingestion.pipeline import ingest_regulatory_instrument

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from persistence.provisioned_postgres import Provisioned

    from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
    from ps_service.ingestion.falkordb_client import FalkorDB, GraphHandle
    from ps_service.logging import LogEmitter

pytestmark = [pytest.mark.falkordb_live, pytest.mark.postgres_live]


class _MakeEmitter(Protocol):
    """Call shape of the shared `make_emitter` fixture (`tests/conftest.py`)."""

    def __call__(self) -> tuple[LogEmitter, Path]: ...


_AUTHORED = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "authored-governance"
        / "authored-governance-seed.json"
    ).read_text("utf-8")
)

# Gives every edge the identity the gateway computes (`TYPE:source->target`); the test-side
# stand-in for "the legacy graph, seen through the gateway" (D2).
_STAMP_IDENTITY = (
    "MATCH (a)-[r]->(b) WHERE r.identity IS NULL "
    "SET r.identity = type(r) + ':' + a.id + '->' + b.id"
)
_EDGE_SHAPE = "MATCH (a)-[r]->(b) RETURN type(r), labels(a)[0], a.id, labels(b)[0], b.id"


class _Stack:
    """The real gateway, and the throwaway graphs of one test."""

    def __init__(self, prov: Provisioned, db: FalkorDB, emitter: LogEmitter) -> None:
        self.prov = prov
        self.emitter = emitter
        self.db = db
        self.store = PsycopgGraphLogStore(prov.state_config())
        self.gateway = GraphWriteGateway(
            log_store=self.store, graph_opener=lambda graph: select_graph(db, graph)
        )
        self.names: list[str] = []

    def graph_name(self, kind: str) -> str:
        """A fresh graph name, deleted when the test ends."""
        name = f"ingest_live_{uuid.uuid4().hex[:8]}_{kind}"
        self.names.append(name)
        return name

    def handle(self, name: str) -> GraphHandle:
        """The real graph of that name."""
        return select_graph(self.db, name)

    def audit(self) -> str:
        """A committed audit row for one run's groups to link to."""
        return committed_audit_event(self.prov)

    def digest(self, name: str) -> str:
        """The canonical digest of the real graph."""
        return canonical_digest(self.handle(name))

    def edge_shape(self, name: str) -> Counter[tuple[object, ...]]:
        """The edge multiset (type, endpoints), identity ignored."""
        rows = cast("list[list[object]]", self.handle(name).query(_EDGE_SHAPE).result_set)
        return Counter(tuple(row) for row in rows)

    def rows(self, name: str, query: str) -> list[list[object]]:
        """Run a read query on the real graph."""
        return cast("list[list[object]]", self.handle(name).query(query).result_set)

    def wipe(self, name: str) -> None:
        """Delete the FalkorDB graph only; its log and applied marker stay."""
        if name in set(self.db.list_graphs()):
            self.db.select_graph(name).delete()

    def close(self) -> None:
        """Delete every graph the test used."""
        for name in self.names:
            self.wipe(name)


@pytest.fixture
def stack(provisioned_graph_log: Provisioned, make_emitter: _MakeEmitter) -> Iterator[_Stack]:
    host, port = falkordb_endpoint()
    emitter, _ = make_emitter()
    made = _Stack(provisioned_graph_log, connect(host=host, port=port), emitter)
    yield made
    made.close()


def _authored_governance_seed() -> InternalRegulationSeed:
    return InternalSeedIngestionAdapter().parse_seed(_AUTHORED)


_SEEDS = [
    pytest.param(full_seed, id="every-label"),
    pytest.param(_authored_governance_seed, id="authored-governance-fixture"),
]


def _ingest_seed(stack: _Stack, seed: InternalRegulationSeed) -> tuple[str, str]:
    native, baseline = stack.graph_name("native"), stack.graph_name("baseline")
    ingest_internal_regulatory_instrument(
        seed,
        submitter=stack.gateway,
        native_graph_name=native,
        baseline_graph_name=baseline,
        audit_event_id=stack.audit(),
    )
    return native, baseline


def _write_legacy_graphs(stack: _Stack, seed: InternalRegulationSeed) -> tuple[str, str]:
    """The graphs the pre-#208 writer built: its own Cypher, edges without `identity`."""
    native, baseline = stack.graph_name("legacy_native"), stack.graph_name("legacy_baseline")
    write_legacy(seed, stack.handle(native), stack.handle(baseline))
    return native, baseline


# --- AC-BI-002 (modulo identity), internal seed ----------------------------------------


@pytest.mark.parametrize("make_seed", _SEEDS)
def test_internal_seed_digest_equals_the_legacy_writers_modulo_edge_identity(
    stack: _Stack, make_seed: Callable[[], InternalRegulationSeed]
) -> None:
    seed = make_seed()
    legacy_native, legacy_baseline = _write_legacy_graphs(stack, seed)
    for name in (legacy_native, legacy_baseline):
        stack.handle(name).query(_STAMP_IDENTITY)

    native, baseline = _ingest_seed(stack, seed)

    assert stack.digest(native) == stack.digest(legacy_native)
    assert stack.digest(baseline) == stack.digest(legacy_baseline)
    assert sum(stack.edge_shape(native).values()) > 0


# --- AC-BI-002 (modulo identity), external native graph --------------------------------

_CELEX = "32024R2847"
_PROPS: dict[str, str | int] = {"text": "t", "citation_ref": "Art. 1", "order": 1}


class _Adapter:
    def __init__(self, structure: FetchedRegulatoryInstrumentStructure) -> None:
        self._structure = structure

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        del identifier
        return self._structure


def _structure(
    *, extra_edges: tuple[StructuralEdge, ...] = ()
) -> FetchedRegulatoryInstrumentStructure:
    return FetchedRegulatoryInstrumentStructure(
        metadata=RegulatoryInstrumentMetadata(
            title="Cyber Resilience Act",
            jurisdiction="EU",
            effective_date=date(2027, 12, 11),
            version="1.0",
            status="active",
            source_type="external",
            instrument_type="regulation",
            celex=_CELEX,
        ),
        nodes=(
            StructuralNode("ARTICLE", f"{_CELEX}#art_1", _PROPS),
            StructuralNode("PARAGRAPH", f"{_CELEX}#art_1.001", _PROPS),
        ),
        edges=(
            StructuralEdge("RegulatoryInstrument", _CELEX, "ARTICLE", f"{_CELEX}#art_1"),
            StructuralEdge("ARTICLE", f"{_CELEX}#art_1", "PARAGRAPH", f"{_CELEX}#art_1.001"),
            *extra_edges,
        ),
    )


class _External:
    """One external regulation under a unique short name (so its native graph is its own)."""

    def __init__(self, stack: _Stack) -> None:
        self.stack = stack
        self.short_name = f"zlive{uuid.uuid4().hex[:8]}"
        self.instrument_id = f"{self.short_name}-1.0"
        self.graph = native_graph_name(self.short_name)
        stack.names.append(self.graph)

    def ingest(self, structure: FetchedRegulatoryInstrumentStructure) -> None:
        ingest_regulatory_instrument(
            _CELEX,
            self.short_name,
            version="1.0",
            adapter=_Adapter(structure),
            graph=self.stack.handle(self.graph),
            submitter=self.stack.gateway,
            audit_event_id=self.stack.audit(),
            emitter=self.stack.emitter,
        )

    def write_legacy(self, name: str, structure: FetchedRegulatoryInstrumentStructure) -> None:
        """The pre-#208 external writer's Cypher for `structure`, into the graph `name`."""
        graph = self.stack.handle(name)
        metadata = structure.metadata
        graph.query(
            "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties",
            {
                "id": self.instrument_id,
                "properties": {
                    "title": metadata.title,
                    "jurisdiction": metadata.jurisdiction,
                    "effective_date": metadata.effective_date.isoformat(),
                    "version": metadata.version,
                    "status": metadata.status,
                    "source_type": metadata.source_type,
                    "instrument_type": metadata.instrument_type,
                    "celex": metadata.celex,
                },
            },
        )
        for node in structure.nodes:
            graph.query(
                f"MERGE (n:{node.element_type} {{id: $id}}) SET n += $properties",
                {"id": node.id, "properties": dict(node.properties)},
            )
        for edge in structure.edges:
            parent = (
                self.instrument_id
                if edge.parent_element_type == "RegulatoryInstrument"
                else edge.parent_id
            )
            graph.query(
                f"MATCH (a:{edge.parent_element_type} {{id: $parent_id}}), "
                f"(b:{edge.child_element_type} {{id: $child_id}}) MERGE (a)-[:HAS]->(b)",
                {"parent_id": parent, "child_id": edge.child_id},
            )


def test_external_native_digest_equals_the_legacy_writers_modulo_edge_identity(
    stack: _Stack,
) -> None:
    run = _External(stack)
    legacy = stack.graph_name("legacy_native")
    run.write_legacy(legacy, _structure())
    stack.handle(legacy).query(_STAMP_IDENTITY)

    run.ingest(_structure())

    assert stack.digest(run.graph) == stack.digest(legacy)
    assert sum(stack.edge_shape(run.graph).values()) == 2


# --- AC-BI-004 (G1): a dangling edge is dropped silently ----------------------------------


def test_an_edge_to_a_node_that_is_nowhere_is_dropped_and_nothing_is_logged_for_it(
    stack: _Stack,
) -> None:
    run = _External(stack)
    dangling = StructuralEdge("ARTICLE", f"{_CELEX}#art_1", "PARAGRAPH", f"{_CELEX}#missing")

    run.ingest(_structure(extra_edges=(dangling,)))

    assert sum(stack.edge_shape(run.graph).values()) == 2
    assert stack.store.last_position(run.graph) == 5  # instrument, two nodes, two surviving edges


# --- AC-BI-005: rerun and replay ----------------------------------------------------------


@pytest.mark.parametrize("make_seed", _SEEDS)
def test_a_second_internal_ingest_changes_nothing_and_logs_nothing(
    stack: _Stack, make_seed: Callable[[], InternalRegulationSeed]
) -> None:
    seed = make_seed()
    native, baseline = _ingest_seed(stack, seed)
    digests = (stack.digest(native), stack.digest(baseline))
    positions = (stack.store.last_position(native), stack.store.last_position(baseline))

    ingest_internal_regulatory_instrument(
        seed,
        submitter=stack.gateway,
        native_graph_name=native,
        baseline_graph_name=baseline,
        audit_event_id=stack.audit(),
    )

    assert (stack.digest(native), stack.digest(baseline)) == digests
    assert (stack.store.last_position(native), stack.store.last_position(baseline)) == positions


def test_a_second_external_ingest_changes_nothing_and_logs_nothing(stack: _Stack) -> None:
    run = _External(stack)
    run.ingest(_structure())
    digest, position = stack.digest(run.graph), stack.store.last_position(run.graph)

    run.ingest(_structure())

    assert stack.digest(run.graph) == digest
    assert stack.store.last_position(run.graph) == position


def test_a_wiped_internal_graph_is_rebuilt_from_the_log_to_the_digest_it_had(
    stack: _Stack,
) -> None:
    native, baseline = _ingest_seed(stack, full_seed())
    expected = {name: stack.digest(name) for name in (native, baseline)}
    for name in expected:
        stack.wipe(name)

    for name in expected:
        stack.gateway.replay_graph(name)

    assert {name: stack.digest(name) for name in expected} == expected


# --- R-embed: AC-BI-003 for embeddings ---------------------------------------------------


def test_an_embedding_less_node_stays_embedding_free_and_a_stored_embedding_survives_reingest(
    stack: _Stack,
) -> None:
    run = _External(stack)
    run.ingest(_structure())
    article, paragraph = f"{_CELEX}#art_1", f"{_CELEX}#art_1.001"
    embedding = (0.5, 0.25, -0.125)
    stack.gateway.submit_group(
        MutationGroup(
            graph=run.graph,
            audit_event_id=stack.audit(),
            primitives=(UpsertNode(label="ARTICLE", id=article, embedding=embedding),),
        )
    )
    with_embedding = stack.digest(run.graph)

    run.ingest(_structure())

    assert stack.digest(run.graph) == with_embedding
    stored = stack.rows(run.graph, f"MATCH (n:ARTICLE {{id: '{article}'}}) RETURN n.embedding")
    assert stored == [[list(embedding)]]
    untouched = stack.rows(
        run.graph, f"MATCH (n:PARAGRAPH {{id: '{paragraph}'}}) RETURN n.embedding IS NULL"
    )
    assert untouched == [[True]]
    assert stack.rows(run.graph, "MATCH (n) WHERE n.embedding IS NOT NULL RETURN count(n)") == [[1]]


# --- F1: a graph written before the gateway ---------------------------------------------


@pytest.mark.parametrize("make_seed", _SEEDS)
def test_reingesting_an_internal_seed_onto_a_legacy_graph_adds_no_parallel_edge(
    stack: _Stack, make_seed: Callable[[], InternalRegulationSeed]
) -> None:
    seed = make_seed()
    legacy_native, legacy_baseline = _write_legacy_graphs(stack, seed)
    shapes = {name: stack.edge_shape(name) for name in (legacy_native, legacy_baseline)}
    expected = _ingest_seed(stack, seed)  # a fresh gateway-written pair, for the digest
    assert all(sum(shape.values()) > 0 for shape in shapes.values())

    ingest_internal_regulatory_instrument(
        seed,
        submitter=stack.gateway,
        native_graph_name=legacy_native,
        baseline_graph_name=legacy_baseline,
        audit_event_id=stack.audit(),
    )

    for name in (legacy_native, legacy_baseline):
        assert stack.edge_shape(name) == shapes[name]  # no parallel edge
    assert stack.digest(legacy_native) == stack.digest(expected[0])
    assert stack.digest(legacy_baseline) == stack.digest(expected[1])
    positions = (
        stack.store.last_position(legacy_native),
        stack.store.last_position(legacy_baseline),
    )
    ingest_internal_regulatory_instrument(
        seed,
        submitter=stack.gateway,
        native_graph_name=legacy_native,
        baseline_graph_name=legacy_baseline,
        audit_event_id=stack.audit(),
    )
    assert (
        stack.store.last_position(legacy_native),
        stack.store.last_position(legacy_baseline),
    ) == positions  # the backfill is idempotent: the second run logs nothing


def test_reingesting_an_external_regulation_onto_a_legacy_graph_adds_no_parallel_edge(
    stack: _Stack,
) -> None:
    run = _External(stack)
    run.write_legacy(run.graph, _structure())
    shape = stack.edge_shape(run.graph)
    assert sum(shape.values()) == 2

    run.ingest(_structure())

    assert stack.edge_shape(run.graph) == shape
    assert stack.rows(run.graph, "MATCH ()-[r]->() WHERE r.identity IS NULL RETURN count(r)") == [
        [0]
    ]
