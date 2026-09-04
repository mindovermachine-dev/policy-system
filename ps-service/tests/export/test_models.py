"""Tests for ps_service.export.models."""

from __future__ import annotations

import dataclasses

import pytest

from ps_service.export.models import (
    InstrumentManifest,
    SerializedEdge,
    SerializedGraph,
    SerializedNode,
)


def _manifest() -> InstrumentManifest:
    return InstrumentManifest(
        instrument_id="CRA-1.0",
        celex="32024R2847",
        title="Cyber Resilience Act",
        short_name="CRA",
        version="1.0",
        source_type="external",
        jurisdiction="EU",
        schema_version="1",
        exported_at="2026-09-04T00:00:00Z",
        baseline_sha256="a" * 64,
        native_sha256="b" * 64,
    )


def test_instrument_manifest_holds_all_fields() -> None:
    manifest = _manifest()

    assert manifest.instrument_id == "CRA-1.0"
    assert manifest.celex == "32024R2847"
    assert manifest.title == "Cyber Resilience Act"
    assert manifest.short_name == "CRA"
    assert manifest.version == "1.0"
    assert manifest.source_type == "external"
    assert manifest.jurisdiction == "EU"
    assert manifest.schema_version == "1"
    assert manifest.exported_at == "2026-09-04T00:00:00Z"
    assert manifest.baseline_sha256 == "a" * 64
    assert manifest.native_sha256 == "b" * 64


def test_instrument_manifest_allows_internal_source_with_no_celex() -> None:
    manifest = dataclasses.replace(_manifest(), source_type="internal", celex=None)

    assert manifest.source_type == "internal"
    assert manifest.celex is None


def test_instrument_manifest_mutation_raises() -> None:
    manifest = _manifest()

    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.title = "Mutated"  # pyright: ignore[reportAttributeAccessIssue]  # asserting frozen-dataclass mutation is rejected at runtime


def _node() -> SerializedNode:
    return SerializedNode(
        label="Capability",
        properties={"id": "cap_1", "name": "Data Encryption", "confidence": 0.9},
    )


def _edge() -> SerializedEdge:
    return SerializedEdge(
        relationship_type="REQUIRES",
        source_label="Obligation",
        source_id="ob_1",
        target_label="Capability",
        target_id="cap_1",
        properties={},
    )


def test_serialized_node_holds_label_and_properties() -> None:
    node = _node()

    assert node.label == "Capability"
    assert node.properties == {"id": "cap_1", "name": "Data Encryption", "confidence": 0.9}


def test_serialized_node_mutation_raises() -> None:
    node = _node()

    with pytest.raises(dataclasses.FrozenInstanceError):
        node.label = "Other"  # pyright: ignore[reportAttributeAccessIssue]  # asserting frozen-dataclass mutation is rejected at runtime


def test_serialized_edge_holds_all_fields() -> None:
    edge = _edge()

    assert edge.relationship_type == "REQUIRES"
    assert edge.source_label == "Obligation"
    assert edge.source_id == "ob_1"
    assert edge.target_label == "Capability"
    assert edge.target_id == "cap_1"
    assert edge.properties == {}


def test_serialized_graph_holds_node_and_edge_tuples() -> None:
    graph = SerializedGraph(nodes=(_node(),), edges=(_edge(),))

    assert graph.nodes == (_node(),)
    assert graph.edges == (_edge(),)


def test_serialized_graph_mutation_raises() -> None:
    graph = SerializedGraph(nodes=(), edges=())

    with pytest.raises(dataclasses.FrozenInstanceError):
        graph.nodes = (_node(),)  # pyright: ignore[reportAttributeAccessIssue]  # asserting frozen-dataclass mutation is rejected at runtime
