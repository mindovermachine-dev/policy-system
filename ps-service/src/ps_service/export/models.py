"""Core types for `ps_service.export` -- the curated-artifact manifest shape.

`InstrumentManifest` is the field-for-field content of one curated
instrument's `manifest.json` (PLAN.md D1) -- an opaque, checksum-verified
description of a `{short}_baseline`/`{short}_native` graph-pair dump, never a
PS Conceptual Model type crossing an LLM boundary, so a plain frozen
dataclass (not Pydantic), matching the existing `company_merge`/
`query_engine` internal-pipeline-plumbing convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class InstrumentManifest:
    """One curated instrument's `manifest.json` content (PLAN.md D1).

    `celex`/`jurisdiction` are `None` for an `internal`-sourced instrument
    (no EU CELEX identifier applies); `schema_version` is compared for exact
    equality against `ps_service.domain_mapper.DOMAIN_SCHEMA_VERSION` at
    restore time (D10) -- no migrate/warn path.
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
    """One node, exactly as read off a live graph via `MATCH (n:{label}) RETURN n`.

    `label` is always singular -- every writer in this codebase mints
    single-labeled nodes (`MERGE (n:{label} {id: ...})`); a node with zero or
    multiple labels is an `ExportSourceGraphError`, not silently coerced.
    `properties["id"]` is the node's stable domain identifier (never
    FalkorDB's internal node id, which this design never reads/writes).
    """

    label: str
    properties: dict[str, SerializedPropertyValue]


@dataclass(frozen=True, slots=True)
class SerializedEdge:
    """One edge, exactly as read via `MATCH (s)-[r:{type}]->(t) RETURN ...`.

    `source_label`/`target_label` let restore's `populate_graph` `MATCH` by
    label without a second lookup -- both are already free on the same
    query that reads the edge (`labels(s)`/`labels(t)`).
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

    D1's replacement for one `DUMP` blob -- what `baseline.json`/
    `native.json` decode to.
    """

    nodes: tuple[SerializedNode, ...]
    edges: tuple[SerializedEdge, ...]
