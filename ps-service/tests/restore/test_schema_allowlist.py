"""Tests for `ps_service.restore.schema_allowlist` (CHANGES2.md §2.3/§2.4).

Mirrors `ingestion/graph_writer.py::_validate_element_types`'s exact shape:
whole-collection validation, raise on the first violation, zero
`graph.query()` calls made by the time this raises (proven here by never
even constructing a graph handle -- `validate_serialized_graph` takes no
graph argument at all, only the parsed `SerializedGraph`).
"""

from __future__ import annotations

import pytest

from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.restore.errors import ArtifactContentRejectedError
from ps_service.restore.schema_allowlist import (
    BASELINE_ALLOWED_LABELS,
    BASELINE_ALLOWED_RELATIONSHIP_TYPES,
    NATIVE_ALLOWED_LABELS,
    NATIVE_ALLOWED_RELATIONSHIP_TYPES,
    validate_serialized_graph,
)


def _graph(
    *, nodes: tuple[SerializedNode, ...] = (), edges: tuple[SerializedEdge, ...] = ()
) -> SerializedGraph:
    return SerializedGraph(nodes=nodes, edges=edges)


def test_baseline_allowed_labels_and_relationship_types_cover_the_documented_schema() -> None:
    assert {
        "RegulatoryInstrument",
        "Role",
        "Requirement",
        "Obligation",
        "Capability",
        "Policy",
        "Standard",
        "Control",
    } == BASELINE_ALLOWED_LABELS
    assert {
        "DEFINES",
        "EXPRESSES",
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
        "GOVERNED_BY",
        "SUPPORTED_BY",
        "IMPLEMENTED_BY",
    } == BASELINE_ALLOWED_RELATIONSHIP_TYPES


def test_native_allowed_labels_and_relationship_types_cover_the_documented_schema() -> None:
    assert {
        "RegulatoryInstrument",
        "TITLE",
        "CHAPTER",
        "SECTION",
        "ARTICLE",
        "PARAGRAPH",
        "ANNEX",
        "RECITAL",
    } == NATIVE_ALLOWED_LABELS
    assert {"HAS"} == NATIVE_ALLOWED_RELATIONSHIP_TYPES


def test_validate_serialized_graph_accepts_an_allow_listed_graph() -> None:
    graph = _graph(
        nodes=(SerializedNode(label="Capability", properties={"id": "cap_1"}),),
        edges=(),
    )

    validate_serialized_graph(
        graph,
        allowed_labels=BASELINE_ALLOWED_LABELS,
        allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
    )


def test_validate_serialized_graph_rejects_a_node_label_outside_the_allow_list() -> None:
    graph = _graph(nodes=(SerializedNode(label="EvilLabel", properties={"id": "x"}),))

    with pytest.raises(ArtifactContentRejectedError):
        validate_serialized_graph(
            graph,
            allowed_labels=BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )


def test_validate_serialized_graph_rejects_an_edge_relationship_type_outside_the_allow_list() -> (
    None
):
    graph = _graph(
        nodes=(
            SerializedNode(label="Capability", properties={"id": "cap_1"}),
            SerializedNode(label="Obligation", properties={"id": "ob_1"}),
        ),
        edges=(
            SerializedEdge(
                relationship_type="EVIL_TYPE",
                source_label="Obligation",
                source_id="ob_1",
                target_label="Capability",
                target_id="cap_1",
                properties={},
            ),
        ),
    )

    with pytest.raises(ArtifactContentRejectedError):
        validate_serialized_graph(
            graph,
            allowed_labels=BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )


def test_validate_serialized_graph_rejects_an_edge_endpoint_label_outside_the_allow_list() -> None:
    graph = _graph(
        edges=(
            SerializedEdge(
                relationship_type="REQUIRES",
                source_label="Obligation",
                source_id="ob_1",
                target_label="EvilLabel",
                target_id="x",
                properties={},
            ),
        ),
    )

    with pytest.raises(ArtifactContentRejectedError):
        validate_serialized_graph(
            graph,
            allowed_labels=BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )


def test_validate_serialized_graph_rejects_a_node_missing_id_property() -> None:
    graph = _graph(nodes=(SerializedNode(label="Capability", properties={"name": "x"}),))

    with pytest.raises(ArtifactContentRejectedError):
        validate_serialized_graph(
            graph,
            allowed_labels=BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )


def test_validate_serialized_graph_rejects_a_node_with_a_non_string_id_property() -> None:
    graph = _graph(nodes=(SerializedNode(label="Capability", properties={"id": 123}),))

    with pytest.raises(ArtifactContentRejectedError):
        validate_serialized_graph(
            graph,
            allowed_labels=BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )


def test_validate_serialized_graph_makes_no_graph_query_calls_before_raising() -> None:
    """Whole-collection validation happens before any write -- proven here by the
    function signature itself taking no graph handle at all, so there is
    nothing to call.
    """
    graph = _graph(nodes=(SerializedNode(label="EvilLabel", properties={"id": "x"}),))

    with pytest.raises(ArtifactContentRejectedError):
        validate_serialized_graph(
            graph,
            allowed_labels=NATIVE_ALLOWED_LABELS,
            allowed_relationship_types=NATIVE_ALLOWED_RELATIONSHIP_TYPES,
        )
