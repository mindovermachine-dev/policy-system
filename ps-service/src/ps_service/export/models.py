"""Core types for `ps_service.export`: the curated-artifact manifest and serialized graph shapes.

`InstrumentManifest` is the field-for-field content of one curated instrument's `manifest.json`:
a checksum-verified description of a `{short}_baseline`/`{short}_native` graph-pair export. It is
plain pipeline plumbing, not a conceptual-model type crossing an LLM boundary, so it is a frozen
dataclass rather than a Pydantic model, as in `company_merge` and `query_engine`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class InstrumentManifest:
    """One curated instrument's `manifest.json` content.

    `celex` and `jurisdiction` are `None` for an `internal`-sourced instrument (no EU CELEX
    identifier applies). `schema_version` is compared for exact equality against
    `ps_service.domain_mapper.DOMAIN_SCHEMA_VERSION` at restore time; there is no migrate or
    warn path.

    Attributes:
        instrument_id: Id of the curated instrument; equals the baseline graph's
            `RegulatoryInstrument.id` and the artifact directory name.
        celex: CELEX identifier, or `None` for internal sources.
        title: Human-readable instrument title.
        short_name: Short slug naming the instrument's graph pair.
        version: Version label of the curated content.
        source_type: `"external"` or `"internal"`.
        jurisdiction: Jurisdiction label, or `None` for internal sources.
        schema_version: Domain schema version the graphs were exported under.
        exported_at: UTC export time as an ISO 8601 string.
        baseline_sha256: Hex SHA-256 of the bytes of `baseline.json`.
        native_sha256: Hex SHA-256 of the bytes of `native.json`.
    """

    instrument_id: str
    celex: str | None
    title: str
    short_name: str
    version: str
    source_type: Literal["external", "internal"]
    jurisdiction: str | None
    schema_version: str
    exported_at: str
    baseline_sha256: str
    native_sha256: str


SerializedPropertyValue = str | float | int | bool | list[float] | list[str]


@dataclass(frozen=True, slots=True)
class SerializedNode:
    """One node, exactly as read off a live graph.

    `label` is always singular because every writer in this codebase creates single-labeled
    nodes; a node with zero or several labels raises `ExportSourceGraphError` instead of being
    coerced. `properties["id"]` is the node's stable domain identifier, never FalkorDB's internal
    node id.

    Attributes:
        label: The node's single label.
        properties: The node's properties keyed by name, including `id`.
    """

    label: str
    properties: dict[str, SerializedPropertyValue]


@dataclass(frozen=True, slots=True)
class SerializedEdge:
    """One edge, exactly as read from a live graph.

    The endpoint labels are stored with the edge so restore can match each endpoint by label and
    id without a second lookup.

    Attributes:
        relationship_type: The edge's relationship type.
        source_label: Label of the source node.
        source_id: Domain `id` of the source node.
        target_label: Label of the target node.
        target_id: Domain `id` of the target node.
        properties: The edge's properties keyed by name; may be empty.
    """

    relationship_type: str
    source_label: str
    source_id: str
    target_label: str
    target_id: str
    properties: dict[str, SerializedPropertyValue]


@dataclass(frozen=True, slots=True)
class SerializedGraph:
    """The complete, portable contents of one FalkorDB graph.

    A portable, driver-independent form of a graph: what `baseline.json` and `native.json`
    decode to.

    Attributes:
        nodes: Every node of the graph.
        edges: Every edge of the graph.
    """

    nodes: tuple[SerializedNode, ...]
    edges: tuple[SerializedEdge, ...]
