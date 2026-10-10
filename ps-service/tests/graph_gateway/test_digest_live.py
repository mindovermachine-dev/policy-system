"""`falkordb_live`: the digest scan against a real FalkorDB (#207 S2 / S2L).

Run with `uv run pytest -m falkordb_live` (see `live_endpoints` for the endpoint variables).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest

from ps_service.graph_gateway.cypher import REPLAY_STATE_LABEL
from ps_service.graph_gateway.digest import DigestSettings, canonical_digest
from ps_service.graph_gateway.models import DeleteEdge, DeleteNode, NodeRef, UpsertEdge, UpsertNode

if TYPE_CHECKING:
    from graph_gateway.live_graphs import LiveGraph, LiveGraphs

pytestmark = pytest.mark.falkordb_live


def _node(node_id: str) -> UpsertNode:
    return UpsertNode(label="Capability", id=node_id, properties={"name": f"name of {node_id}"})


def test_digest_equal_after_delete_and_reinsert_shifts_internal_ids(
    live_graphs: LiveGraphs,
) -> None:
    plain, shifted = live_graphs.new(), live_graphs.new()
    plain.submit(_node("x"), _node("y"), _node("z"))
    shifted.submit(_node("tmp-1"), _node("tmp-2"))
    shifted.submit(DeleteNode(label="Capability", id="tmp-1"))
    shifted.submit(_node("z"), _node("y"), _node("x"))
    shifted.submit(DeleteNode(label="Capability", id="tmp-2"))

    assert [plain.internal_id("Capability", i) for i in "xyz"] != [
        shifted.internal_id("Capability", i) for i in "xyz"
    ]
    assert canonical_digest(plain.handle) == canonical_digest(shifted.handle)


def test_digest_scan_is_chunked_on_the_real_graph(live_graphs: LiveGraphs) -> None:
    graph = live_graphs.new()
    graph.submit(*(_node(f"cap-{i:02d}") for i in range(10)))

    chunked = canonical_digest(graph.handle, DigestSettings(node_chunk_rows=3))

    assert chunked == canonical_digest(graph.handle, DigestSettings(node_chunk_rows=5000))


def test_the_replay_sentinel_is_excluded_by_the_real_scan(live_graphs: LiveGraphs) -> None:
    graph = live_graphs.new()
    graph.submit(_node("cap-1"))
    before = canonical_digest(graph.handle)

    graph.rows(f"CREATE (:{REPLAY_STATE_LABEL} {{position: 3, state: 'in_progress'}})")

    assert canonical_digest(graph.handle) == before


def test_digest_differs_when_a_node_property_differs(live_graphs: LiveGraphs) -> None:
    graph = live_graphs.new()
    graph.submit(_node("cap-1"))
    before = canonical_digest(graph.handle)

    graph.submit(UpsertNode(label="Capability", id="cap-1", properties={"name": "other"}))

    assert canonical_digest(graph.handle) != before


def _edge(identity: str = "e-1", *, reverse: bool = False, **properties: object) -> UpsertEdge:
    policy, standard = NodeRef(label="Policy", id="p-1"), NodeRef(label="Standard", id="s-1")
    source, target = (standard, policy) if reverse else (policy, standard)
    return UpsertEdge(
        type="HAS", identity=identity, source=source, target=target, properties=properties
    )


def _with_edges(live_graphs: LiveGraphs, *edges: UpsertEdge) -> LiveGraph:
    graph = live_graphs.new()
    graph.submit(
        UpsertNode(label="Policy", id="p-1", properties={"name": "a"}),
        UpsertNode(label="Standard", id="s-1", properties={"name": "b"}),
    )
    if edges:
        graph.submit(*edges)
    return graph


def _with_node(
    live_graphs: LiveGraphs, *, embedding: tuple[float, ...] | None = None, **props: object
) -> LiveGraph:
    graph = live_graphs.new()
    graph.submit(UpsertNode(label="Capability", id="cap-1", properties=props, embedding=embedding))
    return graph


def test_digest_differs_when_an_edge_is_added_or_removed(live_graphs: LiveGraphs) -> None:
    bare, linked = _with_edges(live_graphs), _with_edges(live_graphs, _edge())
    assert canonical_digest(bare.handle) != canonical_digest(linked.handle)

    linked.submit(
        DeleteEdge(
            type="HAS",
            identity="e-1",
            source=NodeRef(label="Policy", id="p-1"),
            target=NodeRef(label="Standard", id="s-1"),
        )
    )

    assert canonical_digest(bare.handle) == canonical_digest(linked.handle)


def test_digest_differs_when_relationship_direction_is_reversed(live_graphs: LiveGraphs) -> None:
    forward = _with_edges(live_graphs, _edge())
    backward = _with_edges(live_graphs, _edge(reverse=True))

    assert canonical_digest(forward.handle) != canonical_digest(backward.handle)


def test_parallel_edges_with_different_identities_both_count(live_graphs: LiveGraphs) -> None:
    one = _with_edges(live_graphs, _edge("e-1"))
    two = _with_edges(live_graphs, _edge("e-1"), _edge("e-2"))
    again = _with_edges(live_graphs, _edge("e-2"), _edge("e-1"))

    assert canonical_digest(one.handle) != canonical_digest(two.handle)
    assert canonical_digest(two.handle) == canonical_digest(again.handle)


def test_edge_scan_is_chunked_on_the_real_graph(live_graphs: LiveGraphs) -> None:
    graph = _with_edges(live_graphs, *(_edge(f"e-{i:02d}") for i in range(9)))

    chunked = canonical_digest(graph.handle, DigestSettings(edge_chunk_rows=2))

    assert chunked == canonical_digest(graph.handle, DigestSettings(edge_chunk_rows=5000))


def test_digest_differs_for_a_multi_label_node(live_graphs: LiveGraphs) -> None:
    single, double, reordered, odd = (live_graphs.new() for _ in range(4))
    single.rows("CREATE (:A {id: 'x'})")
    double.rows("CREATE (:A:B {id: 'x'})")
    reordered.rows("CREATE (:B:A {id: 'x'})")
    odd.rows("CREATE (:NotInTheAllowList {id: 'x'})")

    assert canonical_digest(single.handle) != canonical_digest(double.handle)
    assert canonical_digest(double.handle) == canonical_digest(reordered.handle)
    assert canonical_digest(odd.handle) != canonical_digest(single.handle)


def test_digest_distinguishes_int_one_from_float_one_and_bool_true(
    live_graphs: LiveGraphs,
) -> None:
    digests = {
        canonical_digest(_with_node(live_graphs, n=value).handle) for value in (1, 1.0, True)
    }

    assert len(digests) == 3


def test_digest_differs_between_positive_and_negative_zero(live_graphs: LiveGraphs) -> None:
    assert canonical_digest(_with_node(live_graphs, offset=0.0).handle) != canonical_digest(
        _with_node(live_graphs, offset=-0.0).handle
    )
    assert canonical_digest(
        _with_node(live_graphs, embedding=(0.0, 1.0)).handle
    ) != canonical_digest(_with_node(live_graphs, embedding=(-0.0, 1.0)).handle)


def test_digest_tells_embedding_doubles_apart_that_a_plain_reply_prints_alike(
    live_graphs: LiveGraphs,
) -> None:
    base = (0.1, 0.30000000000000004, 1.7976931348623157e308, 5e-324)
    one_ulp = (0.1, math.nextafter(0.30000000000000004, 1.0), 1.7976931348623157e308, 5e-324)
    below_max = (0.1, 0.30000000000000004, math.nextafter(1.7976931348623157e308, 0.0), 5e-324)
    again = _with_node(live_graphs, embedding=base)
    digests = [
        canonical_digest(_with_node(live_graphs, embedding=e).handle)
        for e in (base, one_ulp, below_max)
    ]

    assert len(set(digests)) == 3
    assert canonical_digest(again.handle) == digests[0]


def test_a_requested_checkpoint_equals_the_digest_of_the_real_graph(
    live_graphs: LiveGraphs,
) -> None:
    graph = live_graphs.new()

    outcome = graph.submit(_node("cap-1"), _node("cap-2"), checkpoint_requested=True)

    assert outcome.checkpoint == "recorded"
    stored = graph.store.checkpoints[(graph.name, 2)]
    assert stored.canonical_digest == canonical_digest(graph.handle)
