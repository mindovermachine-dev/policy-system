"""`falkordb_live` + `postgres_live`: replay from the real log into a real FalkorDB.

The log is the real `PsycopgGraphLogStore` as the `ps_state` role; the graph is a real,
throwaway FalkorDB graph. A graph is built through the gateway, deleted from FalkorDB (the
marker still says applied), then rebuilt by `replay_graph` and checked against the checkpoint.

Run with `uv run pytest -m "falkordb_live or postgres_live"`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_postgres import committed_audit_event
from ps_service.graph_gateway.connection import connect, select_graph
from ps_service.graph_gateway.cypher import GRAPH_HOLDS_A_NODE
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import GraphDigestMismatchError
from ps_service.graph_gateway.gateway import GatewaySettings, GraphWriteGateway
from ps_service.graph_gateway.label_allow_list import ALLOWED_RELATIONSHIP_TYPES
from ps_service.graph_gateway.models import (
    DeleteNode,
    MutationGroup,
    NodeRef,
    Primitive,
    StartupReplayReport,
    UpsertEdge,
    UpsertNode,
)
from ps_service.graph_gateway.replay_state import read_replay_state
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persistence.provisioned_postgres import Provisioned

    from graph_gateway.live_graphs import LiveGraphs
    from ps_service.graph_gateway.connection import FalkorDB
    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle

pytestmark = [pytest.mark.falkordb_live, pytest.mark.postgres_live]

_EMBEDDING = (0.1, -0.0, 1.7976931348623157e308, 5e-324, 0.30000000000000004, 1 / 3)


_GRAPHS_CREATED: list[str] = []
"""Every graph name a rig of this module chose, for the module-end cleanup."""


@pytest.fixture(scope="module", autouse=True)
def _delete_every_graph_the_module_created(live_graphs_db: FalkorDB) -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # autouse fixture
    """Delete the module's graphs once its last gateway is stopped.

    The module shares one log, and a later test's gateway recovers the graphs of the earlier tests
    (their applied markers are still in the log), so a graph deleted in its own test's teardown
    can come back; only after the last test is nothing left to bring one back.
    """
    yield
    present = set(live_graphs_db.list_graphs())
    names = [name for name in _GRAPHS_CREATED if name in present]
    if names:
        live_graphs_db.connection.delete(*names)


class _Rig:
    """A gateway over the real log and a real FalkorDB graph of a unique name."""

    def __init__(self, prov: Provisioned, db: FalkorDB) -> None:
        self.prov = prov
        self.db = db
        self.name = f"replay_live_{uuid.uuid4().hex[:10]}"
        _GRAPHS_CREATED.append(self.name)
        self.store = PsycopgGraphLogStore(prov.state_config())
        self.gateways: list[GraphWriteGateway] = []
        self.gateway = self.new_gateway()

    def new_gateway(self) -> GraphWriteGateway:
        gateway = GraphWriteGateway(
            log_store=self.store, graph_opener=lambda graph: select_graph(self.db, graph)
        )
        self.gateways.append(gateway)
        return gateway

    def close(self) -> None:
        """Stop every gateway's reconciler, then delete the graph.

        The reconciler rebuilds a wiped graph whose applied marker is still set (that is the
        behaviour under test), so deleting the graph first would let it come back after the test.
        """
        for gateway in self.gateways:
            gateway.stop_reconciler()
        self.wipe()

    @property
    def handle(self) -> GraphHandle:
        return select_graph(self.db, self.name)

    def submit(self, *primitives: Primitive, checkpoint_requested: bool = False) -> None:
        self.gateway.submit_group(
            MutationGroup(
                graph=self.name,
                audit_event_id=committed_audit_event(self.prov),
                primitives=primitives,
                checkpoint_requested=checkpoint_requested,
            )
        )

    def wipe(self) -> None:
        if self.name in set(self.db.list_graphs()):
            self.db.select_graph(self.name).delete()


@pytest.fixture(scope="module")
def live_graphs_db() -> FalkorDB:
    """A connection to the live FalkorDB for the module-level cleanup."""
    host, port = falkordb_endpoint()
    return connect(host=host, port=port)


@pytest.fixture
def rig(provisioned_graph_log: Provisioned, live_graphs: LiveGraphs) -> Iterator[_Rig]:
    made = _Rig(provisioned_graph_log, live_graphs.new().db)
    yield made
    made.close()


def _build(rig: _Rig) -> None:
    rig.submit(
        UpsertNode(
            label="Capability",
            id="cap-1",
            properties={"name": "a", "weight": 1.0},
            embedding=_EMBEDDING,
        ),
        UpsertNode(label="Capability", id="cap-2", properties={"name": "b"}),
    )
    rig.submit(
        UpsertEdge(
            type=_edge_type(),
            source=NodeRef(label="Capability", id="cap-1"),
            target=NodeRef(label="Capability", id="cap-2"),
            identity="e-1",
            properties={},
        ),
        checkpoint_requested=True,
    )


def _edge_type() -> str:
    return min(ALLOWED_RELATIONSHIP_TYPES)


def test_a_wiped_falkordb_with_applied_markers_is_rebuilt_and_matches_the_checkpoint(
    rig: _Rig,
) -> None:
    _build(rig)
    expected = rig.store.read_digest_checkpoint(rig.name, 3)
    assert expected is not None
    rig.wipe()
    assert rig.store.read_applied_position(rig.name) == 3  # the marker still says applied

    report = rig.new_gateway().replay_graph(rig.name)

    assert (report.verified_position, report.unverified_entries) == (3, 0)
    assert canonical_digest(rig.handle) == expected.canonical_digest


def test_replayed_embedding_doubles_are_bit_identical_after_postgres_and_falkordb(
    rig: _Rig,
) -> None:
    _build(rig)
    before = canonical_digest(rig.handle)
    rig.wipe()

    rig.new_gateway().replay_graph(rig.name)

    assert canonical_digest(rig.handle) == before


def test_entries_after_the_last_checkpoint_are_reported_unverified_on_the_real_log(
    rig: _Rig,
) -> None:
    _build(rig)
    rig.submit(UpsertNode(label="Capability", id="cap-3", properties={"name": "c"}))
    rig.wipe()

    report = rig.new_gateway().replay_graph(rig.name)

    assert (report.verified_position, report.unverified_entries) == (3, 1)
    assert rig.store.read_digest_checkpoint(rig.name, 4) is None  # replay records none


def test_a_wrong_checkpoint_digest_fails_the_replay_on_the_real_log_and_applies_nothing_past_it(
    rig: _Rig,
) -> None:
    rig.submit(UpsertNode(label="Capability", id="cap-1", properties={"name": "a"}))
    rig.store.record_digest_checkpoint(rig.name, 1, "sha256:" + "00" * 32)
    rig.submit(UpsertNode(label="Capability", id="cap-2", properties={"name": "b"}))
    rig.wipe()

    with pytest.raises(GraphDigestMismatchError) as raised:
        rig.new_gateway().replay_graph(rig.name)

    assert (raised.value.graph, raised.value.position) == (rig.name, 1)
    rows = cast(
        "list[list[object]]", rig.handle.query("MATCH (n:Capability) RETURN count(n)").result_set
    )
    assert rows == [[1]]  # cap-2 (position 2) was not applied; the progress record is no node of it


def test_replay_in_small_pages_on_the_real_log_reaches_the_same_digest(rig: _Rig) -> None:
    _build(rig)
    before = canonical_digest(rig.handle)
    rig.wipe()
    paged = GraphWriteGateway(
        log_store=rig.store,
        graph_opener=lambda graph: select_graph(rig.db, graph),
        settings=GatewaySettings(replay_page_size=2),
    )

    report = paged.replay_graph(rig.name)

    assert (report.verified_position, report.pages) == (3, 2)  # pages end at the checkpoint
    assert canonical_digest(rig.handle) == before


@dataclass
class _DyingHandle:
    """A real graph handle that loses the connection after `writes` data writes (a crash)."""

    inner: GraphHandle
    writes: int
    seen: int = field(default=0)

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        if q.startswith("UNWIND"):
            if self.seen >= self.writes:
                message = "connection lost"
                raise ConnectionError(message)
            self.seen += 1
        return self.inner.query(q, params)


def test_replay_interrupted_mid_load_resumes_and_matches(rig: _Rig) -> None:
    _build(rig)
    before = canonical_digest(rig.handle)
    rig.wipe()
    settings = GatewaySettings(replay_page_size=2, batch_size=1)
    dying = GraphWriteGateway(
        log_store=rig.store,
        graph_opener=lambda graph: cast(
            "GraphHandle", _DyingHandle(select_graph(rig.db, graph), writes=2)
        ),
        settings=settings,
    )
    with pytest.raises(ConnectionError):
        dying.replay_graph(rig.name)
    state = read_replay_state(rig.handle)
    assert state is not None
    assert (state.position, state.state) == (2, "in_progress")

    resumed = GraphWriteGateway(
        log_store=rig.store,
        graph_opener=lambda graph: select_graph(rig.db, graph),
        settings=settings,
    )
    report = resumed.replay_graph(rig.name)

    assert report.resumed_from == 3
    assert canonical_digest(rig.handle) == before
    assert read_replay_state(rig.handle) is None


_FLOAT_PROPERTIES: dict[str, object] = {
    "negative_zero": -0.0,
    "one": 1.0,
    "big": 1e22,
    "tiny": 5e-324,
    "tenth": 0.1,
    "exact_int": 2**53,
    "int_max": 2**63 - 1,
    "series": [0.1, -0.0, 1.0],
}


def test_embeddings_survive_postgres_and_falkordb_bit_identically(rig: _Rig) -> None:
    rig.submit(
        UpsertNode(label="Capability", id="cap-1", properties={}, embedding=_EMBEDDING),
        checkpoint_requested=True,
    )
    before = canonical_digest(rig.handle)
    stored = rig.store.read_entries(rig.name)[0]
    assert stored.embedding is not None
    assert [value.hex() for value in stored.embedding] == [value.hex() for value in _EMBEDDING]
    rig.wipe()

    report = rig.new_gateway().replay_graph(rig.name)

    assert report.verified_position == 1
    assert canonical_digest(rig.handle) == before


def test_property_floats_survive_the_jsonb_inline_path(rig: _Rig) -> None:
    rig.submit(
        UpsertNode(label="Capability", id="cap-1", properties=_FLOAT_PROPERTIES),
        checkpoint_requested=True,
    )
    before = canonical_digest(rig.handle)
    content = rig.store.read_entries(rig.name)[0].content
    properties = cast("dict[str, object]", content["properties"])
    assert repr(properties["negative_zero"]) == "-0.0"
    assert isinstance(properties["one"], float)
    assert isinstance(properties["exact_int"], int)
    rig.wipe()

    report = rig.new_gateway().replay_graph(rig.name)

    assert report.verified_position == 1
    assert canonical_digest(rig.handle) == before


# Startup replay against the real log and the real FalkorDB.


def test_wiped_falkordb_with_applied_markers_is_detected_and_rebuilt(rig: _Rig) -> None:
    _build(rig)
    expected = rig.store.read_digest_checkpoint(rig.name, 3)
    assert expected is not None
    rig.wipe()  # a real DEL of the key: the applied marker still says 3
    assert rig.store.read_applied_position(rig.name) == 3

    report = rig.new_gateway().startup_replay()

    assert rig.name in report.replayed
    assert canonical_digest(rig.handle) == expected.canonical_digest
    assert read_replay_state(rig.handle) is None


def test_a_graph_key_that_does_not_exist_answers_the_node_probe_as_empty(rig: _Rig) -> None:
    assert rig.name not in set(rig.db.list_graphs())

    rows = rig.handle.query(GRAPH_HOLDS_A_NODE).result_set

    assert rows == []


def test_startup_replay_leaves_a_healthy_real_graph_alone(rig: _Rig) -> None:
    _build(rig)
    before = canonical_digest(rig.handle)

    report = rig.new_gateway().startup_replay()

    assert rig.name in report.untouched
    assert canonical_digest(rig.handle) == before


def test_startup_replay_finds_and_finishes_a_replay_that_died_half_way(rig: _Rig) -> None:
    _build(rig)
    before = canonical_digest(rig.handle)
    rig.wipe()
    settings = GatewaySettings(replay_page_size=2, batch_size=1)
    dying = GraphWriteGateway(
        log_store=rig.store,
        graph_opener=lambda graph: cast(
            "GraphHandle", _DyingHandle(select_graph(rig.db, graph), writes=2)
        ),
        settings=settings,
    )
    with pytest.raises(ConnectionError):
        dying.replay_graph(rig.name)
    assert rig.store.read_applied_position(rig.name) == 3  # the marker never knew

    report = rig.new_gateway().startup_replay()

    assert rig.name in report.resumed
    assert canonical_digest(rig.handle) == before


def test_a_log_that_nets_to_empty_with_a_head_checkpoint_is_not_replayed(rig: _Rig) -> None:
    rig.submit(UpsertNode(label="Capability", id="cap-1", properties={"name": "a"}))
    rig.submit(DeleteNode(label="Capability", id="cap-1"), checkpoint_requested=True)

    report = rig.new_gateway().startup_replay()

    assert rig.name in report.untouched


def test_startup_replay_reports_every_graph_of_the_log(rig: _Rig) -> None:
    _build(rig)
    rig.wipe()

    report = rig.new_gateway().startup_replay()

    assert isinstance(report, StartupReplayReport)
    assert rig.name in report.replayed


# A failed replay stays gated across a restart.


def _count_elements(rig: _Rig) -> object:
    return rig.handle.query("MATCH (n) RETURN count(n)").result_set


def test_failed_sentinel_survives_a_process_restart(rig: _Rig) -> None:
    rig.submit(UpsertNode(label="Capability", id="cap-1", properties={"name": "a"}))
    rig.store.record_digest_checkpoint(rig.name, 1, "sha256:" + "00" * 32)
    rig.wipe()
    first = rig.new_gateway().startup_replay()
    assert rig.name in first.gated
    state = read_replay_state(rig.handle)
    assert state is not None
    assert (state.state, state.kind, state.position) == ("failed", "GraphDigestMismatchError", 1)
    elements_before = _count_elements(rig)

    restarted = rig.new_gateway()  # a new process: nothing in memory, only the real graph and log
    second = restarted.startup_replay()

    assert rig.name in second.gated
    assert rig.name in restarted.gated_graphs()
    still = read_replay_state(rig.handle)
    assert still == state  # the record is untouched
    assert _count_elements(rig) == elements_before  # nothing re-applied, nothing deleted
