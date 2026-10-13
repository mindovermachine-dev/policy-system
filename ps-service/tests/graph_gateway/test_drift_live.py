"""`falkordb_live`: the drift check against a real FalkorDB (issue #215, S5).

The log store is the in-memory fake; the graph, the writes and the read-only digest are real. It
proves what the fake cannot: the whole-graph digest is readable through `GRAPH.RO_QUERY`, a
write outside the gateway changes it, and a graph key that does not exist digests as the empty
graph (the reply `RO_QUERY` gives for it is mapped by `ReadOnlyGraph`).

Run with `uv run pytest -m falkordb_live ps-service/tests/graph_gateway/test_drift_live.py`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from graph_gateway._fakes import InMemoryGraph, InMemoryGraphLogStore
from graph_gateway.live_endpoints import falkordb_endpoint
from ps_service.graph_gateway.connection import connect, select_graph
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import MutationGroup, UpsertNode
from ps_service.graph_gateway.read_only import open_read_only_database

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.graph_gateway.connection import FalkorDB

pytestmark = pytest.mark.falkordb_live

_AUDIT = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"


class _Live:
    def __init__(self, db: FalkorDB) -> None:
        host, port = falkordb_endpoint()
        self.db = db
        self.name = f"drift_live_{uuid.uuid4().hex[:8]}"
        self.store = InMemoryGraphLogStore()
        reader = open_read_only_database(host, port)
        self.gateway = GraphWriteGateway(
            log_store=self.store,
            graph_opener=lambda graph: select_graph(db, graph),
            read_graph_opener=reader.select_graph,
            reconciler_wait=lambda _delay: True,
        )

    def submit(self, node_id: str, *, checkpoint: bool) -> None:
        self.gateway.submit_group(
            MutationGroup(
                graph=self.name,
                audit_event_id=_AUDIT,
                primitives=(UpsertNode(label="Capability", id=node_id, properties={"name": "n"}),),
                checkpoint_requested=checkpoint,
            )
        )


@pytest.fixture
def live() -> Iterator[_Live]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port)
    scenario = _Live(db)
    yield scenario
    if scenario.name in db.list_graphs():
        db.select_graph(scenario.name).delete()


def test_a_graph_written_through_the_gateway_is_ok_then_an_out_of_band_write_is_drift(
    live: _Live,
) -> None:
    live.submit("cap-1", checkpoint=True)
    assert live.gateway.check_drift(live.name).status == "ok"

    select_graph(live.db, live.name).query("CREATE (:Capability {id: 'sneaked-in'})")

    result = live.gateway.check_drift(live.name)
    assert result.status == "drift"
    assert result.checkpoint_digest != result.live_digest


def test_the_next_gateway_write_without_a_checkpoint_leaves_no_baseline(live: _Live) -> None:
    live.submit("cap-1", checkpoint=True)
    live.submit("cap-2", checkpoint=False)

    assert live.gateway.check_drift(live.name).status == "no_baseline"


def test_a_graph_key_that_does_not_exist_digests_as_the_empty_graph_through_ro_query(
    live: _Live,
) -> None:
    host, port = falkordb_endpoint()
    missing = open_read_only_database(host, port).select_graph(f"absent_{uuid.uuid4().hex[:8]}")

    assert canonical_digest(missing) == canonical_digest(InMemoryGraph())
