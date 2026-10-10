"""`falkordb_live` + `postgres_live`: replay of a CRA x10 log on the real stores (#207 S19).

The dataset is `curated-content/CRA-1.0/baseline.json` (3,482 nodes, 12,727 edges, 522 embeddings
of 3,072 doubles) loaded ten times with the id prefixes `r0..r9`: 162,090 log entries, 127,270
edges, 5,220 embeddings. It is submitted through the real gateway in groups of 500 primitives over
the real `PsycopgGraphLogStore` and a real FalkorDB, then a checkpoint of the original graph is
recorded at the head. Setup is excluded from every timer. The graph is then wiped and replayed.

Measured numbers are printed (`pytest -s`) and recorded in `IMPL_SLICE_19.md`.

Run with `uv run pytest -s -m "falkordb_live or postgres_live" <this file>`.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_postgres import committed_audit_event
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.gateway import DEFAULT_BATCH_SIZE, GatewaySettings, GraphWriteGateway
from ps_service.graph_gateway.models import (
    MutationGroup,
    NodeRef,
    Primitive,
    UpsertEdge,
    UpsertNode,
)
from ps_service.graph_gateway.replay_state import read_replay_state
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.ingestion.falkordb_client import connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persistence.provisioned_postgres import Provisioned

    from ps_service.ingestion.falkordb_client import FalkorDB, GraphHandle

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
    build_seconds: float
    digest_seconds: float

    def gateway(self, *, opener: object | None = None) -> GraphWriteGateway:
        return GraphWriteGateway(
            log_store=self.store,
            graph_opener=cast("object", opener or (lambda name: select_graph(self.db, name))),  # type: ignore[arg-type]
            settings=GatewaySettings(),
        )

    def wipe(self) -> None:
        if self.graph in set(self.db.list_graphs()):
            self.db.select_graph(self.graph).delete()


def _primitives(copy: int, data: dict[str, list[dict[str, object]]]) -> Iterator[Primitive]:
    prefix = f"r{copy}_"
    for node in data["nodes"]:
        properties = dict(cast("dict[str, object]", node["properties"]))
        node_id = prefix + str(properties.pop("id"))
        embedding = cast("list[float] | None", properties.pop("embedding", None))
        yield UpsertNode(
            label=str(node["label"]),
            id=node_id,
            properties=properties,
            embedding=tuple(embedding) if embedding else None,
        )
    for edge in data["edges"]:
        source = NodeRef(label=str(edge["source_label"]), id=prefix + str(edge["source_id"]))
        target = NodeRef(label=str(edge["target_label"]), id=prefix + str(edge["target_id"]))
        yield UpsertEdge(
            type=str(edge["relationship_type"]),
            identity=f"{source.id}|{edge['relationship_type']}|{target.id}",
            source=source,
            target=target,
            properties=cast("dict[str, object]", edge["properties"]),
        )


@pytest.fixture(scope="module")
def dataset(provisioned_graph_log: Provisioned) -> Iterator[_Dataset]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port)
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())
    graph = f"cra_x10_{uuid.uuid4().hex[:8]}"
    data = json.loads(_BASELINE.read_text())
    gateway = GraphWriteGateway(log_store=store, graph_opener=lambda name: select_graph(db, name))
    audit_event_id = committed_audit_event(provisioned_graph_log)
    started = time.perf_counter()
    batch: list[Primitive] = []
    submitted = 0
    for copy in range(_COPIES):
        for primitive in _primitives(copy, data):
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
        edges=len(data["edges"]) * _COPIES,
        embeddings=sum(1 for n in data["nodes"] if n["properties"].get("embedding")) * _COPIES,
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
    # One UNWIND per `batch_size` rows of a run of one kind, plus at most one extra per run
    # boundary; page cuts and checkpoint cuts add boundaries. 10 runs per page is generous.
    bound = math.ceil(dataset.head / DEFAULT_BATCH_SIZE) + 10 * report.pages
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
