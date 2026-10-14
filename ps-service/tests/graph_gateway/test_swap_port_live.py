"""`falkordb_live` twin of the swap key port.

Part 1 proves the real `FalkorDBKeyPort` commands (copy independent of later writes,
an absent key copying as empty, discard of a missing key, an atomic three-key rename that
overwrites, a refused WATCH). Part 2 runs a real swap set through the gateway against a real
FalkorDB (in-memory log) with a reader polling the three graphs. Part 3 (`postgres_live` too)
kills a swap set between the rename and the markers and recovers it over the real
`PsycopgGraphLogStore`.

Run with `uv run pytest -m "falkordb_live or postgres_live"` on this file.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from typing import TYPE_CHECKING, cast

import pytest

from graph_gateway._fakes import InMemoryGraphLogStore
from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_postgres import committed_audit_event
from ps_service.graph_gateway.connection import FalkorDB, connect, select_graph
from ps_service.graph_gateway.default_gateway import FalkorDBKeyPort
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import SwapConflictError
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import (
    GraphLeg,
    MultiGraphGroup,
    UpsertNode,
)
from ps_service.graph_gateway.store import PsycopgGraphLogStore

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence

    from persistence.provisioned_postgres import Provisioned

    from ps_service.graph_gateway.connection import WritableGraph as GraphHandle
    from ps_service.graph_gateway.swap_port import GraphKeyPort, KeySwapper

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_POLL_SECONDS = 0.005
_JOIN_SECONDS = 30.0


class _Keys:
    """A throwaway key namespace on a real FalkorDB, deleted on close."""

    def __init__(self, db: FalkorDB) -> None:
        self.db = db
        self.stem = uuid.uuid4().hex[:10]
        self.port = FalkorDBKeyPort(lambda: db)

    def name(self, part: str) -> str:
        return f"swapport_live_{self.stem}_{part}"

    def seed(self, part: str, *node_ids: str) -> str:
        name = self.name(part)
        graph = select_graph(self.db, name)
        for node_id in node_ids:
            graph.query("CREATE (:Capability {id: $id})", {"id": node_id})
        return name

    def ids(self, name: str) -> set[str]:
        rows = cast(
            "list[list[str]]",
            select_graph(self.db, name).query("MATCH (n:Capability) RETURN n.id").result_set,
        )
        return {row[0] for row in rows}

    def exists(self, name: str) -> bool:
        return self.db.connection.exists(name) == 1

    def close(self) -> None:
        for name in self.db.list_graphs():
            if f"swapport_live_{self.stem}" in name:
                self.db.select_graph(name).delete()


@pytest.fixture
def keys() -> Iterator[_Keys]:
    host, port = falkordb_endpoint()
    holder = _Keys(connect(host=host, port=port))
    yield holder
    holder.close()


@pytest.mark.falkordb_live
def test_a_copy_is_independent_of_later_writes_to_either_side(keys: _Keys) -> None:
    live = keys.seed("live", "a", "b")
    staged = keys.name("staged")

    keys.port.copy(live, staged)
    select_graph(keys.db, live).query("CREATE (:Capability {id: 'later'})")
    select_graph(keys.db, staged).query("CREATE (:Capability {id: 'staged-only'})")

    assert keys.ids(staged) == {"a", "b", "staged-only"}
    assert keys.ids(live) == {"a", "b", "later"}


@pytest.mark.falkordb_live
def test_copying_a_never_touched_key_vivifies_an_empty_graph_and_creates_nothing_else(
    keys: _Keys,
) -> None:
    source = keys.name("never")
    staged = keys.name("staged")

    keys.port.copy(source, staged)

    assert keys.exists(staged)
    assert keys.ids(staged) == set()
    assert not keys.exists(source)


@pytest.mark.falkordb_live
def test_discard_drops_existing_keys_and_ignores_missing_ones(keys: _Keys) -> None:
    present = keys.seed("present", "a")

    keys.port.discard(present, keys.name("missing"))
    keys.port.discard()

    assert not keys.exists(present)


@pytest.mark.falkordb_live
def test_swap_in_renames_three_keys_at_once_overwriting_the_live_ones(keys: _Keys) -> None:
    lives = [keys.seed(part, f"{part}-old") for part in ("n", "b")]
    absent = keys.name("p")
    stageds = [keys.seed(f"{part}_st", f"{part}-new") for part in ("n", "b", "p")]
    pairs = list(zip(stageds, [*lives, absent], strict=True))

    with keys.port.watching([*lives, absent]) as swapper:
        swapper.swap_in(pairs)

    assert keys.ids(lives[0]) == {"n-new"}
    assert keys.ids(lives[1]) == {"b-new"}
    assert keys.ids(absent) == {"p-new"}
    assert not any(keys.exists(name) for name in stageds)


@pytest.mark.falkordb_live
def test_a_write_to_a_watched_key_after_the_watch_began_refuses_the_swap(keys: _Keys) -> None:
    live = keys.seed("live", "old")
    staged = keys.name("staged")

    with keys.port.watching([live]) as swapper:
        keys.port.copy(live, staged)
        select_graph(keys.db, live).query("CREATE (:Capability {id: 'outsider'})")
        with pytest.raises(SwapConflictError):
            swapper.swap_in([(staged, live)])

    assert keys.ids(live) == {"old", "outsider"}
    assert keys.exists(staged)


def _group(graphs: Sequence[str], suffix: str) -> MultiGraphGroup:
    return MultiGraphGroup(
        audit_event_id=_AUDIT_EVENT_ID,
        legs=tuple(
            GraphLeg(
                graph=graph,
                primitives=(UpsertNode(label="Capability", id=f"{graph}-{suffix}"),),
                replace=True,
            )
            for graph in sorted(graphs)
        ),
        apply="swap",
    )


@pytest.mark.falkordb_live
def test_a_reader_polling_the_three_graphs_across_a_real_swap_sees_none_or_all(
    keys: _Keys,
) -> None:
    graphs = [keys.name(part) for part in ("a", "b", "c")]
    gateway = GraphWriteGateway(
        log_store=InMemoryGraphLogStore(),
        graph_opener=lambda graph: select_graph(keys.db, graph),
        key_port=keys.port,
    )
    assert gateway.submit_multi_graph_group(_group(graphs, "old")).status == "applied"
    stop = threading.Event()
    seen_new_then_old: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            states = [keys.ids(graph) == {f"{graph}-new"} for graph in graphs]
            if any(states) and not all(states) and states != sorted(states, reverse=True):
                seen_new_then_old.append(str(states))
            stop.wait(_POLL_SECONDS)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        outcome = gateway.submit_multi_graph_group(_group(graphs, "new"))
    finally:
        stop.set()
        thread.join(_JOIN_SECONDS)

    assert outcome.status == "applied"
    assert seen_new_then_old == []  # a leg read earlier is never new while a later one is old
    assert [keys.ids(graph) for graph in graphs] == [{f"{graph}-new"} for graph in graphs]


class _Killed(BaseException):
    """Raised between the rename and the markers, as a killed process would stop there."""


class _KillAfterRename:
    """A key port that really swaps and then 'dies' before the gateway moves the markers."""

    def __init__(self, inner: GraphKeyPort) -> None:
        self._inner = inner

    def copy(self, source: str, destination: str) -> None:
        self._inner.copy(source, destination)

    def discard(self, *names: str) -> None:
        self._inner.discard(*names)

    @contextlib.contextmanager
    def watching(self, names: Sequence[str]) -> Generator[KeySwapper]:
        with self._inner.watching(names) as swapper:
            yield _DyingSwapper(swapper)


class _DyingSwapper:
    def __init__(self, inner: KeySwapper) -> None:
        self._inner = inner

    def swap_in(self, pairs: Sequence[tuple[str, str]]) -> None:
        self._inner.swap_in(pairs)
        raise _Killed


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_a_set_killed_after_the_rename_is_completed_by_recovery_over_the_real_log(
    keys: _Keys, provisioned_graph_log: Provisioned
) -> None:
    graphs = [keys.name(part) for part in ("a", "b", "c")]
    audit_event_id = committed_audit_event(provisioned_graph_log)
    store = PsycopgGraphLogStore(provisioned_graph_log.state_config())

    def opener(graph: str) -> GraphHandle:
        return select_graph(keys.db, graph)

    group = _group(graphs, "new").model_copy(update={"audit_event_id": audit_event_id})
    dying = GraphWriteGateway(
        log_store=store, graph_opener=opener, key_port=_KillAfterRename(keys.port)
    )

    with pytest.raises(_Killed):
        dying.submit_multi_graph_group(group)
    restarted = GraphWriteGateway(log_store=store, graph_opener=opener, key_port=keys.port)
    recovery = restarted.recover()

    assert recovery.gated == ()
    assert [keys.ids(graph) for graph in graphs] == [{f"{graph}-new"} for graph in graphs]
    assert store.graphs_with_pending_entries() == ()
    replayed = [canonical_digest(opener(graph)) for graph in graphs]
    assert all(digest.startswith("sha256:") for digest in replayed)
