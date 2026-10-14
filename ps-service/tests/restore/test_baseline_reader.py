"""`restore.baseline_reader`: the baseline reader's fixed queries answered from a parsed artifact.

Restore reads the artifact's baseline leg without a staged graph key (the leg is read-only),
so `company_merge.graph_reader.read_baseline_graph` is pointed at a read-only handle over the
parsed `SerializedGraph`. The reader stays the single source of the projections; this handle only
answers its fixed queries.
"""

from __future__ import annotations

from typing import cast

import pytest
from company_merge._seed_merge_graphs import BaselineGraphReader
from graph_gateway._fakes import InMemoryGraph

from ps_service.company_merge import graph_reader
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.restore.baseline_reader import (
    SerializedGraphReader,
    UnansweredBaselineQueryError,
)
from restore._curated import CURATED_IDS, load_curated


def in_memory_graph(serialized: SerializedGraph) -> InMemoryGraph:
    graph = InMemoryGraph()
    for node in serialized.nodes:
        properties = dict(node.properties)
        node_id = cast("str", properties.pop("id"))
        graph.nodes[(node.label, node_id)] = cast("dict[str, object]", properties)
    for edge in serialized.edges:
        key = (
            edge.relationship_type,
            edge.source_label,
            edge.source_id,
            edge.target_label,
            edge.target_id,
            "",
        )
        graph.edges[key] = dict(edge.properties)
    return graph


def _node(label: str, node_id: str, **properties: object) -> SerializedNode:
    return SerializedNode(label=label, properties={"id": node_id, **properties})  # pyright: ignore[reportArgumentType]


def _edge(kind: str, source: tuple[str, str], target: tuple[str, str], **properties: object):
    return SerializedEdge(
        relationship_type=kind,
        source_label=source[0],
        source_id=source[1],
        target_label=target[0],
        target_id=target[1],
        properties=dict(properties),  # pyright: ignore[reportArgumentType]
    )


class _RecordingReader(SerializedGraphReader):
    def __init__(self, graph: SerializedGraph) -> None:
        super().__init__(graph)
        self.asked: set[str] = set()

    def query(self, q: str, params: dict[str, object] | None = None):
        self.asked.add(q)
        return super().query(q, params)


def test_the_query_registry_covers_every_query_constant_of_the_reader() -> None:
    constants = {
        value
        for name, value in vars(graph_reader).items()
        if name.endswith("_QUERY") and isinstance(value, str)
    }

    assert constants == set(graph_reader.BASELINE_QUERIES.values())
    assert len(constants) == 22


def test_reading_a_baseline_asks_exactly_the_registered_queries() -> None:
    reader = _RecordingReader(
        SerializedGraph(nodes=(_node("RegulatoryInstrument", "X-1"),), edges=())
    )

    graph_reader.read_baseline_graph(reader, "X-1")

    assert reader.asked == set(graph_reader.BASELINE_QUERIES.values())


def test_every_registered_query_is_answered() -> None:
    reader = SerializedGraphReader(SerializedGraph(nodes=(), edges=()))

    for query in graph_reader.BASELINE_QUERIES.values():
        assert reader.query(query, {"regulatory_instrument_id": "X-1"}).result_set == []


def test_a_query_the_reader_does_not_issue_is_refused() -> None:
    reader = SerializedGraphReader(SerializedGraph(nodes=(), edges=()))

    with pytest.raises(UnansweredBaselineQueryError):
        reader.query("MATCH (n) DETACH DELETE n")


def test_the_instrument_is_found_only_under_the_requested_id() -> None:
    graph = SerializedGraph(
        nodes=(_node("RegulatoryInstrument", "X-1", title="T", embedding=[0.5]),), edges=()
    )

    found = graph_reader.read_baseline_graph(SerializedGraphReader(graph), "X-1")
    other = graph_reader.read_baseline_graph(SerializedGraphReader(graph), "OTHER")

    assert found.regulatory_instrument_properties == {
        "id": "X-1",
        "title": "T",
        "embedding": [0.5],
    }
    assert other.regulatory_instrument_properties == {}


def test_provenance_edges_hang_off_the_requested_instrument_only() -> None:
    graph = SerializedGraph(
        nodes=(
            _node("RegulatoryInstrument", "X-1"),
            _node("RegulatoryInstrument", "X-2"),
            _node("Role", "role_1", name="r", confidence=0.9),
        ),
        edges=(
            _edge("DEFINES", ("RegulatoryInstrument", "X-1"), ("Role", "role_1"), source_ref="a"),
            _edge("DEFINES", ("RegulatoryInstrument", "X-2"), ("Role", "role_1"), source_ref="b"),
        ),
    )

    read = graph_reader.read_baseline_graph(SerializedGraphReader(graph), "X-1")

    assert [(e.relationship_type, e.target_id, e.source_ref) for e in read.provenance_edges] == [
        ("DEFINES", "role_1", "a")
    ]


def test_an_absent_optional_property_reads_as_missing() -> None:
    graph = SerializedGraph(
        nodes=(_node("Requirement", "req_1", text="t", type="obligation", confidence=0.5),),
        edges=(),
    )

    read = graph_reader.read_baseline_graph(SerializedGraphReader(graph), "X-1")

    assert read.requirement_nodes[0].properties == {
        "text": "t",
        "type": "obligation",
        "confidence": 0.5,
    }


def test_an_edge_whose_endpoint_is_absent_is_not_read() -> None:
    """`populate_graph`'s `MATCH ... MERGE` silently dropped it; so does this reader."""
    graph = SerializedGraph(
        nodes=(_node("Role", "role_1", name="r", confidence=0.9),),
        edges=(_edge("HAS", ("Role", "role_1"), ("Obligation", "missing")),),
    )

    read = graph_reader.read_baseline_graph(SerializedGraphReader(graph), "X-1")

    assert read.bare_edges == ()


def test_a_repeated_edge_is_read_once() -> None:
    graph = SerializedGraph(
        nodes=(
            _node("Role", "role_1", name="r", confidence=0.9),
            _node("Obligation", "ob_1", text="t", confidence=0.9),
        ),
        edges=(
            _edge("HAS", ("Role", "role_1"), ("Obligation", "ob_1")),
            _edge("HAS", ("Role", "role_1"), ("Obligation", "ob_1")),
        ),
    )

    read = graph_reader.read_baseline_graph(SerializedGraphReader(graph), "X-1")

    assert len(read.bare_edges) == 1


@pytest.mark.parametrize("instrument_id", CURATED_IDS)
def test_a_curated_baseline_reads_as_the_old_graph_reader_read_it(instrument_id: str) -> None:
    artifact = load_curated(instrument_id)

    new = graph_reader.read_baseline_graph(SerializedGraphReader(artifact.baseline), instrument_id)
    old = graph_reader.read_baseline_graph(
        BaselineGraphReader(in_memory_graph(artifact.baseline)), instrument_id
    )

    assert new == old
    assert new.regulatory_instrument_properties
