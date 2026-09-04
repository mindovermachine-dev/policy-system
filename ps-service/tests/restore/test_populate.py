"""Unit tests for `ps_service.restore.populate.populate_graph` (CHANGES2.md §2.3).

Fake `_GraphQueryHandle` records every `(query, params)` call -- proving the
batched UNWIND shape (one query per distinct label/relationship-type-triple,
never one per node/edge) and that only label/relationship-type strings are
interpolated, never property values.
"""

from __future__ import annotations

from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.restore.populate import populate_graph


class _FakeQueryResult:
    @property
    def result_set(self) -> list[object]:
        return []


class _RecordingFakeGraphQueryHandle:
    """Satisfies `_GraphQueryHandle` structurally; records every call for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append((q, params))
        return _FakeQueryResult()


def test_populate_graph_issues_one_unwind_create_per_distinct_label() -> None:
    graph = SerializedGraph(
        nodes=(
            SerializedNode(label="Capability", properties={"id": "cap_1", "name": "A"}),
            SerializedNode(label="Capability", properties={"id": "cap_2", "name": "B"}),
            SerializedNode(label="Obligation", properties={"id": "ob_1"}),
        ),
        edges=(),
    )
    fake = _RecordingFakeGraphQueryHandle()

    populate_graph(fake, graph)

    node_calls = [call for call in fake.calls if "CREATE" in call[0]]
    assert len(node_calls) == 2  # one per distinct label, not one per node
    capability_call = next(call for call in node_calls if "Capability" in call[0])
    assert capability_call[0] == "UNWIND $rows AS row CREATE (n:Capability) SET n = row"
    assert capability_call[1] is not None
    assert capability_call[1]["rows"] == [
        {"id": "cap_1", "name": "A"},
        {"id": "cap_2", "name": "B"},
    ]


def test_populate_graph_issues_one_unwind_merge_per_distinct_edge_triple() -> None:
    graph = SerializedGraph(
        nodes=(),
        edges=(
            SerializedEdge(
                relationship_type="REQUIRES",
                source_label="Obligation",
                source_id="ob_1",
                target_label="Capability",
                target_id="cap_1",
                properties={},
            ),
            SerializedEdge(
                relationship_type="REQUIRES",
                source_label="Obligation",
                source_id="ob_2",
                target_label="Capability",
                target_id="cap_2",
                properties={"weight": 2},
            ),
        ),
    )
    fake = _RecordingFakeGraphQueryHandle()

    populate_graph(fake, graph)

    edge_calls = [call for call in fake.calls if "MERGE" in call[0]]
    assert len(edge_calls) == 1  # one per distinct (type, source_label, target_label) triple
    query, params = edge_calls[0]
    assert "MATCH (s:Obligation {id: row.source_id}), (t:Capability {id: row.target_id})" in query
    assert "MERGE (s)-[r:REQUIRES]->(t)" in query
    assert params is not None
    assert params["rows"] == [
        {"source_id": "ob_1", "target_id": "cap_1", "properties": {}},
        {"source_id": "ob_2", "target_id": "cap_2", "properties": {"weight": 2}},
    ]


def test_populate_graph_writes_nothing_for_an_empty_graph() -> None:
    fake = _RecordingFakeGraphQueryHandle()

    populate_graph(fake, SerializedGraph(nodes=(), edges=()))

    assert fake.calls == []
