"""`falkordb_live` + `postgres_live`: replay of a CRA x10 log on the real stores.

The dataset is `curated-content/CRA-1.0/baseline.json` (931 nodes, 1,519 edges, 157 embeddings of
3,072 doubles) loaded ten times with the id prefixes `r0..r9`: 24,500 log entries, 15,190 edges,
1,570 embeddings. Each copy is mapped to primitives the way restore maps a baseline leg. It is
submitted through the real gateway in groups of 500 primitives over the real
`PsycopgGraphLogStore` and a real FalkorDB, then a checkpoint of the original graph is recorded at
the head. Setup is excluded from every timer. The graph is then wiped and replayed.

Measured numbers are printed (`pytest -s`).

Run with `uv run pytest -s -m "falkordb_live or postgres_live" <this file>`.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_postgres import committed_audit_event
from ps_service.export.models import SerializedGraph
from ps_service.export.serialize import parse_serialized_graph_json
from ps_service.graph_gateway.connection import connect, select_graph
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.gateway import DEFAULT_BATCH_SIZE, GatewaySettings, GraphWriteGateway
from ps_service.graph_gateway.models import MutationGroup, UpsertEdge, UpsertNode
from ps_service.graph_gateway.replay_state import read_replay_state
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.restore.leg_primitives import leg_primitives

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from persistence.provisioned_postgres import Provisioned

    from ps_service.graph_gateway.connection import FalkorDB
    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle
    from ps_service.graph_gateway.models import Primitive

pytestmark = [pytest.mark.falkordb_live, pytest.mark.postgres_live]

_BASELINE = Path(__file__).resolve().parents[3] / "curated-content" / "CRA-1.0" / "baseline.json"
_COPIES = 10
_GROUP_SIZE = 500
_BUDGET_SECONDS = 60.0


@dataclass
class _Dataset:
    """The built CRA x10 log: its graph name, the log store and the digest of the original."""

    db: FalkorDB
    store: PsycopgGraphLogStore
    graph: str
    head: int
    original_digest: str
    edges: int
    embeddings: int
    unpaged_statements: int
    node_labels: int
    build_seconds: float
    digest_seconds: float

    def gateway(self, *, opener: Callable[[str], GraphHandle] | None = None) -> GraphWriteGateway:
        return GraphWriteGateway(
            log_store=self.store,
            graph_opener=opener or (lambda name: select_graph(self.db, name)),
            settings=GatewaySettings(),
        )

    def wipe(self) -> None:
        if self.graph in set(self.db.list_graphs()):
            self.db.select_graph(self.graph).delete()


def _prefixed_copy(copy: int, data: SerializedGraph) -> SerializedGraph:
    """`data` with every node id and edge endpoint prefixed `r<copy>_`.

    An edge's authored `identity` names its unprefixed endpoints, so the copy drops it and
    `leg_primitives` derives the identity of the prefixed edge, as restore does for an edge
    without one.
    """
    prefix = f"r{copy}_"
    nodes = tuple(
        replace(node, properties={**node.properties, "id": prefix + str(node.properties["id"])})
        for node in data.nodes
    )
    edges = tuple(
        replace(
            edge,
            source_id=prefix + edge.source_id,
            target_id=prefix + edge.target_id,
            properties={k: v for k, v in edge.properties.items() if k != "identity"},
        )
        for edge in data.edges
    )
    return SerializedGraph(nodes=nodes, edges=edges)


def _primitives(copy: int, data: SerializedGraph) -> tuple[Primitive, ...]:
    """One prefixed copy of the baseline leg as the primitives restore submits for it."""
    return leg_primitives(_prefixed_copy(copy, data))


def _run_key(primitive: Primitive) -> tuple[str, ...]:
    """The applier's run of a node or edge upsert: all node upserts, or one edge type and labels."""
    if isinstance(primitive, UpsertEdge):
        return (primitive.type, primitive.source.label, primitive.target.label)
    return ("node",)


def _statements_in_one_page(primitives: list[Primitive], batch_size: int) -> int:
    """The write statements a replay of `primitives` sends when no page cut splits a run.

    A run is a maximal stretch of node upserts, or of edge upserts of one type between the same
    labels; its rows go out per label, `batch_size` rows a statement. Every node id is distinct,
    so no node run is split at a repeated node.
    """
    total = 0
    for _, run in groupby(primitives, key=_run_key):
        rows = Counter(p.label if isinstance(p, UpsertNode) else "" for p in run)
        total += sum(math.ceil(count / batch_size) for count in rows.values())
    return total


@pytest.fixture(scope="module")
def dataset(provisioned_graph_log: Provisioned) -> Iterator[_Dataset]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port)
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    graph = f"cra_x10_{uuid.uuid4().hex[:8]}"
    data = parse_serialized_graph_json(_BASELINE.read_bytes())
    gateway = GraphWriteGateway(log_store=store, graph_opener=lambda name: select_graph(db, name))
    audit_event_id = committed_audit_event(provisioned_graph_log)
    started = time.perf_counter()
    batch: list[Primitive] = []
    logged: list[Primitive] = []
    submitted = 0
    for copy in range(_COPIES):
        for primitive in _primitives(copy, data):
            logged.append(primitive)
            batch.append(primitive)
            if len(batch) == _GROUP_SIZE:
                gateway.submit_group(
                    MutationGroup(
                        graph=graph, audit_event_id=audit_event_id, primitives=tuple(batch)
                    )
                )
                submitted += len(batch)
                batch = []
    if batch:
        gateway.submit_group(
            MutationGroup(
                graph=graph,
                audit_event_id=audit_event_id,
                primitives=tuple(batch),
                checkpoint_requested=False,
            )
        )
        submitted += len(batch)
    build_seconds = time.perf_counter() - started
    handle = select_graph(db, graph)
    started = time.perf_counter()
    digest = canonical_digest(handle)
    digest_seconds = time.perf_counter() - started
    head = store.last_position(graph)
    assert head == submitted
    store.record_digest_checkpoint(graph, head, digest)
    built = _Dataset(
        db=db,
        store=store,
        graph=graph,
        head=head,
        original_digest=digest,
        edges=len(data.edges) * _COPIES,
        embeddings=sum(1 for n in data.nodes if n.properties.get("embedding")) * _COPIES,
        unpaged_statements=_statements_in_one_page(logged, DEFAULT_BATCH_SIZE),
        node_labels=len({node.label for node in data.nodes}),
        build_seconds=build_seconds,
        digest_seconds=digest_seconds,
    )
    print(  # noqa: T201 - the measurement is the product of this module
        f"\n[perf] built CRA x{_COPIES}: {head} entries, {built.edges} edges, "
        f"{built.embeddings} embeddings; setup {build_seconds:.1f}s; "
        f"digest alone {digest_seconds:.1f}s"
    )
    yield built
    built.wipe()


@dataclass
class _CountingHandle:
    """A real graph handle that counts the statements it forwards, by kind."""

    inner: GraphHandle
    counts: Counter[str] = field(default_factory=Counter)

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        kind = "write" if q.startswith("UNWIND") else q.split(" ", 1)[0]
        self.counts[kind] += 1
        return self.inner.query(q, params)


def test_cra_times_ten_replays_within_sixty_seconds(dataset: _Dataset) -> None:
    dataset.wipe()
    counting: dict[str, _CountingHandle] = {}

    def opener(name: str) -> GraphHandle:
        counting.setdefault(name, _CountingHandle(select_graph(dataset.db, name)))
        return cast("GraphHandle", counting[name])

    gateway = dataset.gateway(opener=opener)

    started = time.perf_counter()
    report = gateway.replay_graph(dataset.graph)
    seconds = time.perf_counter() - started

    counts = counting[dataset.graph].counts
    print(  # noqa: T201 - the measurement is the product of this module
        f"\n[perf] replay {seconds:.1f}s (budget {_BUDGET_SECONDS:.0f}s); pages {report.pages}; "
        f"statements {dict(counts)}"
    )
    assert report.verified_position == dataset.head  # the digest at the head matched
    assert canonical_digest(select_graph(dataset.db, dataset.graph)) == dataset.original_digest
    assert seconds < _BUDGET_SECONDS
    assert read_replay_state(select_graph(dataset.db, dataset.graph)) is None


def test_replay_issues_a_bounded_number_of_write_statements(dataset: _Dataset) -> None:
    dataset.wipe()
    handles: dict[str, _CountingHandle] = {}

    def opener(name: str) -> GraphHandle:
        handles.setdefault(name, _CountingHandle(select_graph(dataset.db, name)))
        return cast("GraphHandle", handles[name])

    report = dataset.gateway(opener=opener).replay_graph(dataset.graph)

    writes = handles[dataset.graph].counts["write"]
    # The log's runs as one page, plus what each page cut adds: a cut splits at most one run,
    # so it adds at most one statement per label that run holds.
    bound = dataset.unpaged_statements + (report.pages - 1) * dataset.node_labels
    print(f"\n[perf] write statements {writes} for {dataset.head} entries (bound {bound})")  # noqa: T201
    assert writes <= bound
    assert writes < dataset.head / 10  # nowhere near one statement per entry


def test_digest_alone_stays_inside_the_budget(dataset: _Dataset) -> None:
    started = time.perf_counter()
    digest = canonical_digest(select_graph(dataset.db, dataset.graph))
    seconds = time.perf_counter() - started
    print(f"\n[perf] digest alone {seconds:.1f}s")  # noqa: T201
    assert digest == dataset.original_digest
    assert seconds < _BUDGET_SECONDS


@dataclass
class _DyingHandle:
    """A real graph handle that loses its connection after `writes` data writes (a crash)."""

    inner: GraphHandle
    writes: int
    seen: int = 0

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        if q.startswith("UNWIND"):
            if self.seen >= self.writes:
                message = "connection lost"
                raise ConnectionError(message)
            self.seen += 1
        return self.inner.query(q, params)


def test_a_restart_resumes_an_interrupted_cra_times_ten_replay(dataset: _Dataset) -> None:
    dataset.wipe()

    def dying_opener(name: str) -> GraphHandle:
        return cast("GraphHandle", _DyingHandle(select_graph(dataset.db, name), writes=100))

    dying = dataset.gateway(opener=dying_opener)
    with pytest.raises(ConnectionError):
        dying.replay_graph(dataset.graph)
    state = read_replay_state(select_graph(dataset.db, dataset.graph))
    assert state is not None
    assert state.state == "in_progress"
    assert 0 < state.position < dataset.head

    restarted = dataset.gateway()  # a new process: only the real log and the real graph remain
    started = time.perf_counter()
    report = restarted.startup_replay()
    seconds = time.perf_counter() - started

    print(f"\n[perf] resumed from {state.position} to {dataset.head} in {seconds:.1f}s")  # noqa: T201
    assert report.resumed == (dataset.graph,)
    assert canonical_digest(select_graph(dataset.db, dataset.graph)) == dataset.original_digest
    assert read_replay_state(select_graph(dataset.db, dataset.graph)) is None
    again = dataset.gateway().startup_replay()  # and a clean restart leaves it alone
    assert dataset.graph in again.untouched
