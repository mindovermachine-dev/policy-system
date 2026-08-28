"""ps_service.ingestion core types — the shapes that cross the Adapter ->
Ingestion-core boundary (`RegulationMetadata`, `FetchedRegulationStructure`)
plus the Ingestion pipeline's own structural/result types.

See `docs/architecture/ps-service-container-architecture.md`'s Regulation
node and Native Structural Graph attribute tables (lines 183-222) for the
field-level contract these types encode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

RegulationStatus = Literal["active", "superseded", "vacated"]
SourceType = Literal["external", "internal"]
InstrumentType = Literal["regulation", "directive", "national_transposition"]


class RegulationMetadata(BaseModel):
    """Bibliographic metadata for a Regulation node — crosses the Adapter ->
    Ingestion-core boundary, so modeled as Pydantic (frozen) per L2 Data
    Modeling's principle for boundary-crossing fixed-shape entities, even
    though Regulation isn't in L2's named PS-Conceptual-Model-type list
    (Role/Requirement/Obligation/Capability/Policy/Standard/Control) — a
    reasoned extension of that principle (PLAN_REVIEWED.md §2.2, Open
    Question 4).
    """

    model_config = ConfigDict(frozen=True)
    title: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=1)
    # `date`, not `str`: CA doc line 200 types this `date`. Pydantic parses
    # an ISO-8601 string ("2024-10-17") into a real `date` automatically, so
    # this is free boundary validation — a malformed/unparseable date string
    # fails at model-construction time instead of silently passing through
    # as an opaque string (PLAN_REVIEWED.md §2.2, S3 fix).
    effective_date: date
    version: str = Field(min_length=1)
    status: RegulationStatus
    source_type: SourceType
    instrument_type: InstrumentType | None = None

    @model_validator(mode="after")
    def _check_instrument_type_matches_source_type(self) -> Self:
        """`instrument_type` is required for external sources and must be
        absent for internal ones (`ps-domain-concepts.md`, Regulatory
        instrument -> Properties)."""
        if self.source_type == "external" and self.instrument_type is None:
            raise ValueError("instrument_type is required when source_type is 'external'")
        if self.source_type == "internal" and self.instrument_type is not None:
            raise ValueError("instrument_type must be absent when source_type is 'internal'")
        return self


@dataclass(frozen=True, slots=True)
class StructuralNode:
    """One native structural element. Not a PS Conceptual Model type — CA
    doc line 164 explicitly says this shape is adapter-defined, not a fixed
    project-wide schema — so a plain typed dataclass, not Pydantic."""

    element_type: str  # Cellar/ELI vocabulary: CHAPTER/SECTION/ARTICLE/PARAGRAPH/ANNEX/RECITAL
    id: str
    properties: dict[str, str | int]  # text, citation_ref, order, heading (all str|int, no Any)


@dataclass(frozen=True, slots=True)
class StructuralEdge:
    """One parent -> child edge in the native structural graph."""

    parent_element_type: str  # "RegulatoryInstrument" for top-level children, else a StructuralNode.element_type
    parent_id: str
    child_element_type: str
    child_id: str


@dataclass(frozen=True, slots=True)
class FetchedRegulationStructure:
    """The Adapter's single return type — `FetchRegulationStructure`'s
    post-condition ('structural text held in memory, ready for
    PersistNativeStructuralGraph') confirms fetch+parse are one Adapter
    action, not two."""

    metadata: RegulationMetadata
    nodes: tuple[StructuralNode, ...]
    edges: tuple[StructuralEdge, ...]


@dataclass(frozen=True, slots=True)
class ReachabilityCount:
    """Per-label result of `verify_structural_graph_reachable`: how many
    nodes of one label exist in the graph versus how many are actually
    reachable from the Regulation node."""

    total: int
    reachable: int


@dataclass(frozen=True, slots=True)
class IngestResult:
    """`ingest_regulation()`'s return value — the outcome of one run."""

    regulation_id: str
    run_id: str
    counts: dict[str, ReachabilityCount]
