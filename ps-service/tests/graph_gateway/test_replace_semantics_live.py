"""`falkordb_live` twin of the replace semantics.

The fast tests interpret the gateway's own
Cypher templates by regex; only a real FalkorDB proves that the expansion's stamps and deletes
leave a graph whose canonical digest equals a fresh restore of the same content, including over
edges that carry no `identity` (a graph written before the gateway). The log store stays the
in-memory fake.

Deselected by default -- run with `uv run pytest -m falkordb_live ps-service/tests/graph_gateway`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graph_gateway._fakes import InMemoryGraphLogStore
from ps_service.graph_gateway.connection import select_graph
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.models import (
    GraphLeg,
    MultiGraphGroup,
    NodeRef,
    Primitive,
    UpsertEdge,
    UpsertNode,
)

if TYPE_CHECKING:
    from graph_gateway.live_graphs import LiveGraph, LiveGraphs

pytestmark = pytest.mark.falkordb_live

_EVENT_ONE = "3f2b8c1e-5d4a-4b7e-9a61-0c2d7e8f9a10"
_EVENT_TWO = "9b1e2f3a-4c5d-4e6f-8a7b-1c2d3e4f5a6b"


def _policy(node_id: str, **properties: object) -> UpsertNode:
    return UpsertNode(label="Policy", id=node_id, properties=properties)


def _standard(node_id: str, **properties: object) -> UpsertNode:
    return UpsertNode(label="Standard", id=node_id, properties=properties)


def _has(source: str, target: str, **properties: object) -> UpsertEdge:
    return UpsertEdge(
        type="HAS",
        identity=f"HAS:{source}->{target}",
        source=NodeRef(label="Policy", id=source),
        target=NodeRef(label="Standard", id=target),
        properties=properties,
    )


def _restore(replaced: LiveGraph, merged: LiveGraph, *primitives: Primitive, event: str) -> None:
    gateway = GraphWriteGateway(
        log_store=InMemoryGraphLogStore(),
        graph_opener=lambda name: select_graph(replaced.db, name),
    )
    gateway.submit_multi_graph_group(
        MultiGraphGroup(
            audit_event_id=event,
            legs=(
                GraphLeg(graph=replaced.name, primitives=primitives, replace=True),
                GraphLeg(graph=merged.name, primitives=(_policy("merge-marker"),)),
            ),
        )
    )


def test_restoring_over_a_legacy_copy_equals_restoring_into_an_empty_graph(
    live_graphs: LiveGraphs,
) -> None:
    content = (
        _policy("p-1", name="one"),
        _standard("s-1", name="std"),
        _has("p-1", "s-1", source_ref="x"),
    )
    fresh, fresh_merge = live_graphs.new(), live_graphs.new()
    _restore(fresh, fresh_merge, *content, event=_EVENT_ONE)
    legacy, legacy_merge = live_graphs.new(), live_graphs.new()
    legacy.rows(
        "CREATE (:Policy {id: 'p-1', name: 'old', note: 'surplus'}), "
        "(:Policy {id: 'p-2'}), (:Standard {id: 's-1', name: 'std'})"
    )
    legacy.rows(
        "MATCH (p:Policy {id: 'p-1'}), (s:Standard {id: 's-1'}) "
        "CREATE (p)-[:HAS {source_ref: 'y'}]->(s)"
    )
    legacy.rows("MATCH (p:Policy {id: 'p-2'}), (s:Standard {id: 's-1'}) CREATE (p)-[:HAS]->(s)")

    _restore(legacy, legacy_merge, *content, event=_EVENT_TWO)

    assert canonical_digest(legacy.handle) == canonical_digest(fresh.handle)
    assert legacy.rows("MATCH ()-[r]->() WHERE r.identity IS NULL RETURN count(r)") == [[0]]
