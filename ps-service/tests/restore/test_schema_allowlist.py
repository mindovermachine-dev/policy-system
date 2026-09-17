"""Tests for `ps_service.restore.schema_allowlist` (CHANGES2.md §2.3/§2.4).

Mirrors `ingestion/graph_writer.py::_validate_element_types`'s exact shape:
whole-collection validation, raise on the first violation, zero
`graph.query()` calls made by the time this raises (proven here by never
even constructing a graph handle -- `validate_serialized_graph` takes no
graph argument at all, only the parsed `SerializedGraph`).
"""

from __future__ import annotations

from typing import get_args

import pytest

from ps_service.export.models import SerializedEdge, SerializedGraph, SerializedNode
from ps_service.ingestion.adapters.internal_seed.models import EdgeType, NodeLabel
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


def _edge(
    relationship_type: str, source: tuple[str, str], target: tuple[str, str]
) -> SerializedEdge:
    return SerializedEdge(
        relationship_type=relationship_type,
        source_label=source[0],
        source_id=source[1],
        target_label=target[0],
        target_id=target[1],
        properties={},
    )


_CELLAR_ELI_STRUCTURAL_LABELS = frozenset(
    {"TITLE", "CHAPTER", "SECTION", "ARTICLE", "PARAGRAPH", "ANNEX", "RECITAL"}
)
_CELLAR_ELI_STRUCTURAL_RELATIONSHIP_TYPES = frozenset({"HAS"})
_DOCUMENTED_EDGE_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    # Triples of relationship type, source label, target label -- the documented
    # endpoint pairs in docs/artifacts/ps-domain-concepts.md.
    ("DEFINES", "RegulatoryInstrument", "Role"),
    ("EXPRESSES", "RegulatoryInstrument", "Requirement"),
    ("HAS", "Role", "Obligation"),
    ("SATISFIED_BY", "Requirement", "Obligation"),
    ("REQUIRES", "Obligation", "Capability"),
    ("GOVERNED_BY", "Capability", "Policy"),
    ("SUPPORTED_BY", "Policy", "Standard"),
    ("IMPLEMENTED_BY", "Standard", "Control"),
    ("VERIFIED_BY", "RiskPath", "Control"),
    ("OWNS", "PracticeArea", "Policy"),
    ("COVERS", "PracticeArea", "Capability"),
    ("MITIGATED_BY", "RiskPath", "Capability"),
)


def test_baseline_allow_lists_equal_the_internal_seed_vocabulary() -> None:
    """AC-BI-003 (baseline half): the hand-written baseline lists must equal the
    intake boundary's `NodeLabel`/`EdgeType` vocabulary exactly, so any future
    vocabulary drift on either side fails here and is widened deliberately.
    """
    assert frozenset[str](get_args(NodeLabel)) == BASELINE_ALLOWED_LABELS
    assert frozenset[str](get_args(EdgeType)) == BASELINE_ALLOWED_RELATIONSHIP_TYPES


def test_baseline_allow_lists_accept_the_practice_area_and_risk_path_classification_layer() -> None:
    """AC-BI-001: the GH #93 classification layer (endpoint pairs per
    `docs/artifacts/ps-domain-concepts.md`) passes baseline content validation.
    """
    graph = _graph(
        nodes=(
            SerializedNode(label="PracticeArea", properties={"id": "pa_1"}),
            SerializedNode(label="RiskPath", properties={"id": "rp_1"}),
            SerializedNode(label="Capability", properties={"id": "cap_1"}),
            SerializedNode(label="Policy", properties={"id": "pol_1"}),
            SerializedNode(label="Control", properties={"id": "ctl_1"}),
        ),
        edges=(
            _edge("COVERS", ("PracticeArea", "pa_1"), ("Capability", "cap_1")),
            _edge("OWNS", ("PracticeArea", "pa_1"), ("Policy", "pol_1")),
            _edge("MITIGATED_BY", ("RiskPath", "rp_1"), ("Capability", "cap_1")),
            _edge("VERIFIED_BY", ("RiskPath", "rp_1"), ("Control", "ctl_1")),
        ),
    )

    validate_serialized_graph(
        graph,
        allowed_labels=BASELINE_ALLOWED_LABELS,
        allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
    )


def test_native_allow_lists_equal_the_internal_seed_vocabulary_united_with_cellar_eli_members() -> (
    None
):
    """AC-BI-003 (native half): the native lists equal the full intake vocabulary
    united with the Cellar/ELI structural members (`ingestion/graph_writer.py::
    _KNOWN_ELEMENT_TYPES`, private -- restated here as a literal on purpose).
    """
    expected_labels = frozenset[str](get_args(NodeLabel)) | _CELLAR_ELI_STRUCTURAL_LABELS
    expected_relationship_types = (
        frozenset[str](get_args(EdgeType)) | _CELLAR_ELI_STRUCTURAL_RELATIONSHIP_TYPES
    )

    assert expected_labels == NATIVE_ALLOWED_LABELS
    assert expected_relationship_types == NATIVE_ALLOWED_RELATIONSHIP_TYPES


def test_native_allow_lists_accept_every_internal_seed_node_label_and_edge_type() -> None:
    """AC-BI-002: an internal instrument's native leg carries the full intake
    vocabulary verbatim -- all node labels and all edge types (documented
    endpoint pairs per `docs/artifacts/ps-domain-concepts.md`) pass native
    content validation.
    """
    node_labels: tuple[str, ...] = get_args(NodeLabel)
    nodes = tuple(
        SerializedNode(label=label, properties={"id": f"{label.lower()}_1"})
        for label in node_labels
    )
    edges = tuple(
        _edge(relationship_type, (source, f"{source.lower()}_1"), (target, f"{target.lower()}_1"))
        for relationship_type, source, target in _DOCUMENTED_EDGE_ENDPOINTS
    )
    assert {edge.relationship_type for edge in edges} == set(get_args(EdgeType))

    validate_serialized_graph(
        _graph(nodes=nodes, edges=edges),
        allowed_labels=NATIVE_ALLOWED_LABELS,
        allowed_relationship_types=NATIVE_ALLOWED_RELATIONSHIP_TYPES,
    )


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

    with pytest.raises(
        ArtifactContentRejectedError, match=r"node label 'EvilLabel' is not in the allow-list"
    ):
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

    with pytest.raises(
        ArtifactContentRejectedError,
        match=r"relationship_type 'EVIL_TYPE' is not in the allow-list",
    ):
        validate_serialized_graph(
            graph,
            allowed_labels=BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )


def test_validate_serialized_graph_rejects_a_relationship_type_outside_the_native_allow_list() -> (
    None
):
    """AC-BI-006 on the native leg: widening never admits an unknown type."""
    graph = _graph(
        nodes=(
            SerializedNode(label="ARTICLE", properties={"id": "art_1"}),
            SerializedNode(label="PARAGRAPH", properties={"id": "par_1"}),
        ),
        edges=(_edge("EVIL_TYPE", ("ARTICLE", "art_1"), ("PARAGRAPH", "par_1")),),
    )

    with pytest.raises(
        ArtifactContentRejectedError,
        match=r"relationship_type 'EVIL_TYPE' is not in the allow-list",
    ):
        validate_serialized_graph(
            graph,
            allowed_labels=NATIVE_ALLOWED_LABELS,
            allowed_relationship_types=NATIVE_ALLOWED_RELATIONSHIP_TYPES,
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

    with pytest.raises(
        ArtifactContentRejectedError,
        match=r"edge target_label 'EvilLabel' is not in the allow-list",
    ):
        validate_serialized_graph(
            graph,
            allowed_labels=BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )


def test_validate_serialized_graph_rejects_a_node_missing_id_property() -> None:
    graph = _graph(nodes=(SerializedNode(label="Capability", properties={"name": "x"}),))

    with pytest.raises(
        ArtifactContentRejectedError,
        match=r"node with label 'Capability' has a missing or non-string 'id' property: None",
    ):
        validate_serialized_graph(
            graph,
            allowed_labels=BASELINE_ALLOWED_LABELS,
            allowed_relationship_types=BASELINE_ALLOWED_RELATIONSHIP_TYPES,
        )


def test_validate_serialized_graph_rejects_a_node_with_a_non_string_id_property() -> None:
    graph = _graph(nodes=(SerializedNode(label="Capability", properties={"id": 123}),))

    with pytest.raises(
        ArtifactContentRejectedError,
        match=r"node with label 'Capability' has a missing or non-string 'id' property: 123",
    ):
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

    with pytest.raises(
        ArtifactContentRejectedError, match=r"node label 'EvilLabel' is not in the allow-list"
    ):
        validate_serialized_graph(
            graph,
            allowed_labels=NATIVE_ALLOWED_LABELS,
            allowed_relationship_types=NATIVE_ALLOWED_RELATIONSHIP_TYPES,
        )
