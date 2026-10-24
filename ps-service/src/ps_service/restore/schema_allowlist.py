"""Allow-lists for the node labels and relationship types a restore artifact may carry.

A restore artifact is parsed from a file (curated-content repository, or a
`POST /restorations` upload). Unlike a live FalkorDB read, its label and relationship type
strings are untrusted content, and labels and relationship types cannot be parameterized in
Cypher. Checksum verification catches accidental corruption in transit; these allow-lists are
the separate defense against a crafted artifact with a consistent checksum that attempts
label or relationship-type Cypher injection, so a tampered artifact cannot inject unverified
content.

The four lists are deliberately hand-written literals (defense in depth): this sink-side layer
stays independent of the intake boundary's `internal_seed/models.py::NodeLabel`/`EdgeType`
`Literal` types, so widening the intake vocabulary never silently widens what an uploaded
artifact may inject. They are pinned to that vocabulary by `tests/restore/test_schema_allowlist.py`;
drift fails the test and is widened here on purpose.
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
        "Control",  # Policy/Standard/Control: internal-source only
        "PracticeArea",
        "RiskPath",  # PracticeArea/RiskPath: internal-source classification layer
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
        "VERIFIED_BY",
        "OWNS",
        "COVERS",
        "MITIGATED_BY",  # VERIFIED_BY/OWNS/COVERS/MITIGATED_BY: classification layer
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
        # Internal-source native shape: the internal-seed adapter's `{short}_native` graph
        # carries the customer's own submission verbatim, using the full internal-seed intake
        # vocabulary (`internal_seed/models.py::NodeLabel`/`EdgeType`, pinned by
        # `tests/restore/test_schema_allowlist.py`), which differs from the structural element
        # vocabulary of Cellar/ELI above.
        "Role",
        "Requirement",
        "Obligation",
        "Capability",
        "Policy",
        "Standard",
        "Control",  # Policy/Standard/Control: authored governance layer
        "PracticeArea",
        "RiskPath",  # PracticeArea/RiskPath: internal-source classification layer
    }
)  # = internal-seed NodeLabel | ingestion/graph_writer.py::_KNOWN_ELEMENT_TYPES
NATIVE_ALLOWED_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        "HAS",
        # Internal-source native shape -- see NATIVE_ALLOWED_LABELS.
        "DEFINES",
        "EXPRESSES",
        "SATISFIED_BY",
        "REQUIRES",
        "GOVERNED_BY",
        "SUPPORTED_BY",
        "IMPLEMENTED_BY",  # GOVERNED_BY/SUPPORTED_BY/IMPLEMENTED_BY: governance layer
        "VERIFIED_BY",
        "OWNS",
        "COVERS",
        "MITIGATED_BY",  # VERIFIED_BY/OWNS/COVERS/MITIGATED_BY: classification layer
    }
)  # = internal-seed EdgeType (Cellar/ELI's HAS is already a member)

CAPABILITY_STATUSES: frozenset[str] = frozenset({"active", "deprecated"})
# = the `Capability.status` enum of the domain schema, pinned by
# `tests/restore/test_schema_allowlist.py`.


def _validate_node_id(node_label: str, properties: Mapping[str, object]) -> None:
    """Reject a node whose `id` property is missing or not a string."""
    node_id = properties.get("id")
    if not isinstance(node_id, str):
        raise ArtifactContentRejectedError(
            f"node with label {node_label!r} has a missing or non-string 'id' property: {node_id!r}"
        )


def _validate_capability_status(node_label: str, properties: Mapping[str, object]) -> None:
    """Reject a `Capability` whose `status` is present and outside the schema enum.

    An absorbed Capability is deleted and leaves a `MergedCapability` marker, so no
    artifact legitimately carries a merge status. Any value the schema does not define
    would reach the graph through `ON CREATE SET n += properties`; this is the one
    property-level rule. Label and relationship allow-lists already exclude the
    `Merged*` markers and the `ResolvedCapability` marker, so no artifact can restore one.
    """
    status = properties.get("status")
    if node_label == "Capability" and status is not None and status not in CAPABILITY_STATUSES:
        raise ArtifactContentRejectedError(
            f"Capability {properties.get('id')!r} has unsupported status {status!r}"
        )


def validate_serialized_graph(
    graph: SerializedGraph,
    *,
    allowed_labels: frozenset[str],
    allowed_relationship_types: frozenset[str],
) -> None:
    """Validate a whole parsed artifact leg against its allow-lists, before any write.

    The entire node and edge collection is checked first and the first violation raises; no
    graph call is made.

    Args:
        graph: The parsed native or baseline leg.
        allowed_labels: Labels a node, and an edge's source and target, may carry.
        allowed_relationship_types: Relationship types an edge may carry.

    Returns:
        None; returns normally only when every node and edge passes.

    Raises:
        ArtifactContentRejectedError: a node or edge label, or a relationship type, is outside
            its allow-list; a node's `properties["id"]` is missing or not a string; or a
            `Capability` has a `status` outside the schema enum.
    """
    for node in graph.nodes:
        if node.label not in allowed_labels:
            raise ArtifactContentRejectedError(
                f"node label {node.label!r} is not in the allow-list {sorted(allowed_labels)!r}"
            )
        _validate_node_id(node.label, node.properties)
        _validate_capability_status(node.label, node.properties)

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
