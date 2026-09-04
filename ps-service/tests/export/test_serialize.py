"""Tests for `ps_service.export.serialize` (PLAN.md Slice 2.2, CHANGES2.md §3.3).

The fake `_GraphQueryHandle` dispatches by a distinctive substring of each
query -- mirroring `tests/company_merge/test_graph_reader.py`'s own
`_ScriptedFakeGraph` dispatch style -- rather than importing this module's
private query constants.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from ps_service.export.errors import ExportSourceGraphError
from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.export.serialize import (
    checksum_bytes,
    parse_serialized_graph_json,
    serialize_graph,
    to_json_bytes,
)


def test_checksum_bytes_matches_hashlib_sha256_hexdigest() -> None:
    blob = b"abc"

    assert checksum_bytes(blob) == hashlib.sha256(blob).hexdigest()


def test_checksum_bytes_differs_for_different_byte_strings() -> None:
    assert checksum_bytes(b"abc") != checksum_bytes(b"xyz")


class _FakeQueryResult:
    """Satisfies the module-internal `_QueryResult` Protocol structurally."""

    def __init__(self, result_set: list[list[object]]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[list[object]]:
        return self._result_set


class _ScriptedFakeGraphQueryHandle:
    """Satisfies `_GraphQueryHandle` structurally -- dispatches by a distinctive
    substring of the query text, mirroring `tests/company_merge/
    test_graph_reader.py`'s own `_ScriptedFakeGraph` convention.
    """

    def __init__(
        self,
        *,
        labels: list[list[object]],
        relationship_types: list[list[object]],
        node_rows_by_label: dict[str, list[list[object]]],
        edge_rows_by_type: dict[str, list[list[object]]],
        total_node_count: int,
    ) -> None:
        self._labels = labels
        self._relationship_types = relationship_types
        self._node_rows_by_label = node_rows_by_label
        self._edge_rows_by_type = edge_rows_by_type
        self._total_node_count = total_node_count

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if q == "CALL db.labels()":
            return _FakeQueryResult(self._labels)
        if q == "CALL db.relationshipTypes()":
            return _FakeQueryResult(self._relationship_types)
        if q == "MATCH (n) RETURN count(n)":
            return _FakeQueryResult([[self._total_node_count]])
        for label, rows in self._node_rows_by_label.items():
            if q == f"MATCH (n:{label}) RETURN labels(n), properties(n)":
                return _FakeQueryResult(rows)
        for relationship_type, rows in self._edge_rows_by_type.items():
            if f"[r:{relationship_type}]" in q:
                return _FakeQueryResult(rows)
        raise AssertionError(f"unexpected query: {q!r}")


def test_serialize_graph_reads_nodes_and_edges_via_generic_label_enumeration() -> None:
    graph = _ScriptedFakeGraphQueryHandle(
        labels=[["Test"]],
        relationship_types=[["LINKS_TO"]],
        node_rows_by_label={
            "Test": [
                [["Test"], {"id": "x", "name": "y"}],
                [["Test"], {"id": "z"}],
            ]
        },
        edge_rows_by_type={
            "LINKS_TO": [[["Test"], "x", ["Test"], "z", {"n": 1}]],
        },
        total_node_count=2,
    )

    result = serialize_graph(graph)

    assert len(result.nodes) == 2
    assert all(node.label == "Test" for node in result.nodes)
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.relationship_type == "LINKS_TO"
    assert edge.source_label == "Test"
    assert edge.source_id == "x"
    assert edge.target_label == "Test"
    assert edge.target_id == "z"
    assert edge.properties == {"n": 1}


def test_serialize_graph_raises_on_multi_labeled_node() -> None:
    graph = _ScriptedFakeGraphQueryHandle(
        labels=[["A"], ["B"]],
        relationship_types=[],
        node_rows_by_label={
            "A": [[["A", "B"], {"id": "x"}]],
            "B": [[["A", "B"], {"id": "x"}]],
        },
        edge_rows_by_type={},
        total_node_count=1,
    )

    with pytest.raises(ExportSourceGraphError):
        serialize_graph(graph)


def test_serialize_graph_raises_on_zero_labeled_node() -> None:
    graph = _ScriptedFakeGraphQueryHandle(
        labels=[],
        relationship_types=[],
        node_rows_by_label={},
        edge_rows_by_type={},
        total_node_count=1,
    )

    with pytest.raises(ExportSourceGraphError):
        serialize_graph(graph)


def test_to_json_bytes_produces_deterministic_sorted_json_with_trailing_newline() -> None:
    graph = SerializedGraph(
        nodes=(
            SerializedNode(label="Test", properties={"id": "z"}),
            SerializedNode(label="Test", properties={"id": "a"}),
        ),
        edges=(
            SerializedEdge(
                relationship_type="LINKS_TO",
                source_label="Test",
                source_id="a",
                target_label="Test",
                target_id="z",
                properties={},
            ),
        ),
    )

    blob = to_json_bytes(graph)

    assert blob.endswith(b"\n")
    document = json.loads(blob)
    assert [node["properties"]["id"] for node in document["nodes"]] == ["a", "z"]
    assert to_json_bytes(graph) == to_json_bytes(graph)  # deterministic re-encoding


def test_parse_serialized_graph_json_is_the_inverse_of_to_json_bytes() -> None:
    graph = SerializedGraph(
        nodes=(SerializedNode(label="Test", properties={"id": "x", "n": 1.5}),),
        edges=(
            SerializedEdge(
                relationship_type="LINKS_TO",
                source_label="Test",
                source_id="x",
                target_label="Test",
                target_id="x",
                properties={"weight": 2},
            ),
        ),
    )

    round_tripped = parse_serialized_graph_json(to_json_bytes(graph))

    assert round_tripped == graph
