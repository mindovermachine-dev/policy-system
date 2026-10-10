"""`falkordb_live` tests: the gateway's Cypher against a real FalkorDB (issue #206).

The fake graph of the unit tests interprets the exported templates; only a real FalkorDB proves
the templates are valid Cypher and behave as the fake assumes. The log store stays the in-memory
fake (Postgres has its own live file, `test_gateway_live.py`). Each test uses a throwaway graph
that is deleted before and after.

Deselected by default -- run with `uv run pytest -m falkordb_live` against a FalkorDB at
127.0.0.1:6379, or the one named by `PS_TEST_FALKORDB_HOST` and `PS_TEST_FALKORDB_PORT`. Not
runnable against a plain Redis (no graph module).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions

from graph_gateway._fakes import InMemoryGraphLogStore
from graph_gateway.live_endpoints import falkordb_endpoint
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.graph_reader import read_nodes
from ps_service.graph_gateway.models import (
    DeleteEdge,
    DeleteNode,
    GroupOutcome,
    MergeProperty,
    MutationGroup,
    NodeRef,
    Primitive,
    RemoveProperty,
    UpsertEdge,
    UpsertNode,
)
from ps_service.ingestion.falkordb_client import FalkorDB, connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ps_service.ingestion.falkordb_client import GraphHandle

pytestmark = pytest.mark.falkordb_live

_AUDIT_EVENT_ID = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_EMBEDDING = (0.1, -0.0, 1.7976931348623157e308, 5e-324, 0.30000000000000004)


class _Live:
    """A gateway over the in-memory log store and one real, throwaway FalkorDB graph."""

    def __init__(self, db: FalkorDB, name: str) -> None:
        self.name = name
        self.db = db
        self.store = InMemoryGraphLogStore()
        self.gateway = GraphWriteGateway(
            log_store=self.store, graph_opener=lambda graph: select_graph(db, graph)
        )

    @property
    def graph(self) -> GraphHandle:
        return select_graph(self.db, self.name)

    def submit(self, *primitives: Primitive) -> GroupOutcome:
        return self.gateway.submit_group(
            MutationGroup(graph=self.name, audit_event_id=_AUDIT_EVENT_ID, primitives=primitives)
        )

    def rows(self, query: str, params: dict[str, object] | None = None) -> list[list[object]]:
        return cast("list[list[object]]", self.graph.query(query, params).result_set)


@pytest.fixture
def live() -> Iterator[_Live]:
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port)
    name = f"gateway_live_{uuid.uuid4().hex[:8]}"
    yield _Live(db, name)
    if name in db.list_graphs():
        db.select_graph(name).delete()


def test_node_primitives_apply_and_read_back_as_the_fake_assumes(live: _Live) -> None:
    live.submit(
        UpsertNode(
            label="Capability",
            id="cap-1",
            properties={"name": "a", "tags": ["x", "y"], "weight": 1},
            embedding=_EMBEDDING,
        )
    )
    live.submit(MergeProperty(label="Capability", id="cap-1", properties={"status": "active"}))
    live.submit(RemoveProperty(label="Capability", id="cap-1", keys=("tags", "absent")))

    # FalkorDB stores the doubles exactly but prints them with 15 digits in every reply, so the
    # value is proven twice: server-side equality (exact) and the gateway's exact-float read.
    assert live.rows(
        "MATCH (n:Capability {id: $id}) RETURN n.embedding = $embedding",
        {"id": "cap-1", "embedding": list(_EMBEDDING)},
    ) == [[True]]
    stored = read_nodes(live.graph, [("Capability", "cap-1")], 500)[("Capability", "cap-1")]
    assert (stored["name"], stored["status"], stored["weight"]) == ("a", "active", 1)
    assert "tags" not in stored
    assert [float(v).hex() for v in cast("list[float]", stored["embedding"])] == [
        v.hex() for v in _EMBEDDING
    ]

    live.submit(DeleteNode(label="Capability", id="cap-1"))
    assert live.rows("MATCH (n:Capability) RETURN n.id") == []


def test_repeating_a_group_is_a_noop_against_real_state_reads(live: _Live) -> None:
    node = UpsertNode(
        label="Capability",
        id="cap-1",
        properties={"name": "a", "tags": ["x", "y"], "n": 1, "f": 1.0, "b": True},
        embedding=_EMBEDDING,
    )
    live.submit(node)

    assert live.submit(node).status == "unchanged"
    assert live.submit(
        UpsertNode(label="Capability", id="cap-1", properties={"n": 1.0})
    ).status == ("applied")
    assert live.store.last_position(live.name) == 2


def test_edges_merge_by_identity_and_delete_only_the_named_edge(live: _Live) -> None:
    source = NodeRef(label="Policy", id="p-1")
    target = NodeRef(label="Standard", id="s-1")

    def edge(identity: str, **props: object) -> UpsertEdge:
        return UpsertEdge(
            type="HAS", identity=identity, source=source, target=target, properties=props
        )

    live.submit(UpsertNode(label="Policy", id="p-1"), UpsertNode(label="Standard", id="s-1"))
    live.submit(edge("e-1", weight=1), edge("e-2", weight=1))
    live.submit(edge("e-1", weight=2))
    assert live.rows("MATCH ()-[r:HAS]->() RETURN count(r)") == [[2]]
    assert live.submit(edge("e-1", weight=2)).status == "unchanged"

    live.submit(DeleteEdge(type="HAS", identity="e-1", source=source, target=target))

    assert live.rows("MATCH ()-[r:HAS]->() RETURN r.identity") == [["e-2"]]


def test_interleaved_labels_apply_in_label_batches_and_a_replay_writes_nothing(
    live: _Live,
) -> None:
    nodes = [
        UpsertNode(label=("Capability", "Policy")[i % 2], id=f"n-{i}", properties={"rank": i})
        for i in range(1201)
    ]

    assert live.submit(*nodes).status == "applied"

    assert live.rows("MATCH (n:Capability) RETURN count(n)") == [[601]]
    assert live.rows("MATCH (n:Policy) RETURN count(n)") == [[600]]
    assert live.store.read_applied_position(live.name) == 1201
    assert live.submit(*nodes).status == "unchanged"


def test_first_write_indexes_id_and_a_flushed_graph_is_indexed_again(live: _Live) -> None:
    live.submit(UpsertNode(label="Capability", id="cap-1"))

    indexes = live.rows("CALL db.indexes()")
    assert [row[0] for row in indexes] == ["Capability"]
    assert "id" in cast("list[object]", indexes[0][1])

    live.db.select_graph(live.name).delete()  # a flushed graph loses its indexes with its data
    live.submit(UpsertNode(label="Capability", id="cap-2"))

    assert [row[0] for row in live.rows("CALL db.indexes()")] == ["Capability"]


def test_creating_an_id_index_twice_answers_already_indexed(live: _Live) -> None:
    live.submit(UpsertNode(label="Capability", id="cap-1"))

    with pytest.raises(redis.exceptions.ResponseError, match=r"(?i)already indexed"):
        live.graph.query("CREATE INDEX FOR (n:Capability) ON (n.id)")
