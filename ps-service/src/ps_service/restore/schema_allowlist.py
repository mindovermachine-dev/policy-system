"""L2 Query Safety: 'allow-list labels/relationship types before interpolation'.

A restore artifact is parsed from a file (curated-content/git repo, or a
POST /restorations upload) -- unlike a live FalkorDB read, its label/
relationship_type strings are untrusted content that `populate_graph`
f-string-interpolates into Cypher (labels/relationship types cannot be
parameterized). Checksum verification (D9) catches accidental corruption in
transit; this allow-list is the separate, deliberate defense against a
maliciously-crafted-but-checksum-consistent artifact attempting label/
relationship-type Cypher injection -- AC-BI-010's "a tampered artifact
can't inject unverified content" reads, under this redesign, as covering
CONTENT safety, not just byte-integrity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.restore.errors import ArtifactContentRejectedError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.export.models import SerializedGraph

BASELINE_ALLOWED_LABELS: frozenset[str] = frozenset(
    {
        "RegulatoryInstrument",
        "Role",
        "Requirement",
        "Obligation",
        "Capability",
        "Policy",
        "Standard",
        "Control",  # Policy/Standard/Control: internal-source only, D15
    }
)
BASELINE_ALLOWED_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        "DEFINES",
        "EXPRESSES",
        "HAS",
        "SATISFIED_BY",
        "REQUIRES",
        "GOVERNED_BY",
        "SUPPORTED_BY",
        "IMPLEMENTED_BY",
    }
)
NATIVE_ALLOWED_LABELS: frozenset[str] = frozenset(
    {
        "RegulatoryInstrument",
        "TITLE",
        "CHAPTER",
        "SECTION",
        "ARTICLE",
        "PARAGRAPH",
        "ANNEX",
        "RECITAL",
    }
)  # mirrors ingestion/graph_writer.py::_KNOWN_ELEMENT_TYPES + RegulatoryInstrument
NATIVE_ALLOWED_RELATIONSHIP_TYPES: frozenset[str] = frozenset({"HAS"})


def _validate_node_id(node_label: str, properties: Mapping[str, object]) -> None:
    node_id = properties.get("id")
    if not isinstance(node_id, str):
        raise ArtifactContentRejectedError(
            f"node with label {node_label!r} has a missing or non-string 'id' property: {node_id!r}"
        )


def validate_serialized_graph(
    graph: SerializedGraph,
    *,
    allowed_labels: frozenset[str],
    allowed_relationship_types: frozenset[str],
) -> None:
    """Whole-collection validation, before any write.

    Mirrors `ingestion/graph_writer.py::_validate_element_types`'s exact
    shape (validate the ENTIRE nodes+edges collection first, raise on the
    first violation, zero `graph.query()` calls made by the time this
    raises).

    Raises `ArtifactContentRejectedError` on: a node/edge label or
    relationship_type outside its allow-list; a node whose
    `properties["id"]` is missing or not a string (`populate_graph`'s
    edge-matching step depends on it existing).
    """
    for node in graph.nodes:
        if node.label not in allowed_labels:
            raise ArtifactContentRejectedError(
                f"node label {node.label!r} is not in the allow-list {sorted(allowed_labels)!r}"
            )
        _validate_node_id(node.label, node.properties)

    for edge in graph.edges:
        if edge.relationship_type not in allowed_relationship_types:
            raise ArtifactContentRejectedError(
                f"relationship_type {edge.relationship_type!r} is not in the allow-list "
                f"{sorted(allowed_relationship_types)!r}"
            )
        if edge.source_label not in allowed_labels:
            raise ArtifactContentRejectedError(
                f"edge source_label {edge.source_label!r} is not in the allow-list "
                f"{sorted(allowed_labels)!r}"
            )
        if edge.target_label not in allowed_labels:
            raise ArtifactContentRejectedError(
                f"edge target_label {edge.target_label!r} is not in the allow-list "
                f"{sorted(allowed_labels)!r}"
            )
