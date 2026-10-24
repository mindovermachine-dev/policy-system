"""`falkordb_live`: a write whose reply was lost converges on a real FalkorDB.

The real graph executes every query; a thin wrapper then raises the client timeout the driver
would have raised, so the gateway cannot tell the request ran. Whole-graph copy and rename go
through the real key port the same way. Each test uses throwaway graphs that are deleted after.

Deselected by default; run with `uv run pytest -m falkordb_live` against the FalkorDB named by
`PS_TEST_FALKORDB_HOST` and `PS_TEST_FALKORDB_PORT` (default 127.0.0.1:6379).
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING

import pytest
import redis.exceptions

from graph_gateway._fakes import InMemoryGraphLogStore
from graph_gateway.live_endpoints import FALKORDB_TEST_TIMEOUTS, falkordb_endpoint
from ps_service.graph_gateway.connection import FalkorDB, connect, select_graph
from ps_service.graph_gateway.default_gateway import FalkorDBKeyPort
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import GraphApplyError
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import (
    GraphLeg,
    MultiGraphGroup,
    MutationGroup,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence

    from ps_service.graph_gateway.connection import GraphQueryResult
    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle
    from ps_service.graph_gateway.swap_port import KeySwapper

pytestmark = pytest.mark.falkordb_live

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_LOST_REPLY = redis.exceptions.TimeoutError("Timeout reading from socket")


class _Space:
    """A throwaway namespace of graph keys on a real FalkorDB, deleted on close."""

    def __init__(self, db: FalkorDB) -> None:
        self.db = db
        self.stem = f"ambiguous_live_{uuid.uuid4().hex[:8]}"

    def name(self, part: str) -> str:
        return f"{self.stem}_{part}"

    def digest(self, name: str) -> str:
        return canonical_digest(select_graph(self.db, name))

    def keys(self) -> list[str]:
        return [name for name in self.db.list_graphs() if name.startswith(self.stem)]

    def close(self) -> None:
        for name in self.keys():
            self.db.select_graph(name).delete()


@pytest.fixture
def space() -> Iterator[_Space]:
    host, port = falkordb_endpoint()
    holder = _Space(connect(host=host, port=port, **FALKORDB_TEST_TIMEOUTS))
    yield holder
    holder.close()


class _LosesTheFirstWriteReply:
    """A graph handle whose first write runs on the server and then raises a client timeout."""

    def __init__(self, inner: GraphHandle) -> None:
        self._inner = inner
        self.writes = 0
        self._armed = True

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        result = self._inner.query(q, params)
        if self._armed and any(clause in q.upper() for clause in ("MERGE", "SET", "DELETE")):
            self.writes += 1
            self._armed = False
            raise _LOST_REPLY
        return result


def _group(graph: str) -> MutationGroup:
    return MutationGroup(
        graph=graph,
        audit_event_id=_AUDIT_EVENT_ID,
        primitives=(
            UpsertNode(label="Policy", id="p-1", properties={"name": "p"}),
            UpsertNode(label="Standard", id="s-1", properties={"name": "s"}),
            UpsertEdge(
                type="HAS",
                identity="e-1",
                source=NodeRef(label="Policy", id="p-1"),
                target=NodeRef(label="Standard", id="s-1"),
                properties={"weight": 1},
            ),
        ),
    )


def test_a_write_whose_reply_was_lost_leaves_the_digest_of_a_fault_free_submission(
    space: _Space,
) -> None:
    faulty, clean = space.name("faulty"), space.name("clean")
    losing = _LosesTheFirstWriteReply(select_graph(space.db, faulty))
    gateway = GraphWriteGateway(
        log_store=InMemoryGraphLogStore(),
        graph_opener=lambda name: losing if name == faulty else select_graph(space.db, name),
        sleep=lambda _seconds: None,
    )

    lost = gateway.submit_group(_group(faulty))
    gateway.submit_group(_group(clean))

    rows = select_graph(space.db, faulty).query("MATCH (n) RETURN count(n)").result_set
    edges = select_graph(space.db, faulty).query("MATCH ()-[r:HAS]->() RETURN count(r)").result_set
    assert losing.writes == 1
    assert lost.status == "applied"
    assert (rows, edges) == ([[2]], [[1]])
    assert space.digest(faulty) == space.digest(clean)


class _LosesTheReplyOf:
    """A key port over the real one that can lose the reply of the copy or of the rename."""

    def __init__(self, inner: FalkorDBKeyPort, *, step: str) -> None:
        self._inner = inner
        self._step = step

    def copy(self, source: str, destination: str) -> None:
        self._inner.copy(source, destination)
        if self._step == "copy":
            self._step = ""
            raise _LOST_REPLY

    def discard(self, *names: str) -> None:
        self._inner.discard(*names)

    @contextlib.contextmanager
    def watching(self, names: Sequence[str]) -> Generator[KeySwapper]:
        with self._inner.watching(names) as swapper:
            yield _RenameThatLosesItsReply(swapper, self) if self._step == "rename" else swapper

    def rename_done(self) -> None:
        self._step = ""


class _RenameThatLosesItsReply:
    def __init__(self, inner: KeySwapper, port: _LosesTheReplyOf) -> None:
        self._inner = inner
        self._port = port

    def swap_in(self, pairs: Sequence[tuple[str, str]]) -> None:
        self._inner.swap_in(pairs)
        self._port.rename_done()
        raise _LOST_REPLY


def _set(graphs: Sequence[str]) -> MultiGraphGroup:
    return MultiGraphGroup(
        audit_event_id=_AUDIT_EVENT_ID,
        legs=tuple(
            GraphLeg(
                graph=graph,
                primitives=(UpsertNode(label="Capability", id=f"{graph}-new"),),
                replace=True,
            )
            for graph in sorted(graphs)
        ),
        apply="swap",
    )


def _seeded_gateway(space: _Space, key_port: object, graphs: Sequence[str]) -> GraphWriteGateway:
    store = InMemoryGraphLogStore()
    seed = GraphWriteGateway(
        log_store=store, graph_opener=lambda name: select_graph(space.db, name)
    )
    for graph in graphs:
        seed.submit_group(
            MutationGroup(
                graph=graph,
                audit_event_id=_AUDIT_EVENT_ID,
                primitives=(UpsertNode(label="Capability", id=f"{graph}-old"),),
            )
        )
    return GraphWriteGateway(
        log_store=store,
        graph_opener=lambda name: select_graph(space.db, name),
        key_port=key_port,  # pyright: ignore[reportArgumentType]  # structural stand-in
        sleep=lambda _seconds: None,
    )


def test_a_graph_copy_whose_reply_was_lost_leaves_the_live_graphs_unchanged(
    space: _Space,
) -> None:
    graphs = [space.name(part) for part in ("a", "b")]
    gateway = _seeded_gateway(
        space, _LosesTheReplyOf(FalkorDBKeyPort(lambda: space.db), step="copy"), graphs
    )
    before = [space.digest(graph) for graph in graphs]

    with pytest.raises(GraphApplyError):
        gateway.submit_multi_graph_group(_set(graphs))

    assert [space.digest(graph) for graph in graphs] == before
    assert sorted(space.keys()) == sorted(graphs)  # no staged key is left behind


def test_a_rename_whose_reply_was_lost_is_completed_by_recovery_to_the_fault_free_digests(
    space: _Space,
) -> None:
    graphs = [space.name(part) for part in ("a", "b")]
    reference = [space.name(part) for part in ("ref_a", "ref_b")]
    real_port = FalkorDBKeyPort(lambda: space.db)
    expected = _seeded_gateway(space, real_port, reference)
    expected.submit_multi_graph_group(_set(reference))
    gateway = _seeded_gateway(space, _LosesTheReplyOf(real_port, step="rename"), graphs)

    outcome = gateway.submit_multi_graph_group(_set(graphs))
    recovery = gateway.recover()

    assert outcome.status == "committed_apply_pending"
    assert recovery.gated == ()
    assert sorted(space.keys()) == sorted([*graphs, *reference])
    for graph in graphs:
        nodes = select_graph(space.db, graph).query("MATCH (n:Capability) RETURN n.id").result_set
        assert nodes == [[f"{graph}-new"]]
