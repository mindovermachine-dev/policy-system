"""Core ingestion types.

The shapes that cross the adapter to ingestion-core boundary (`RegulatoryInstrumentMetadata`,
`FetchedRegulatoryInstrumentStructure`) plus the pipeline's own structural and result types. The
field-level contract they encode is the RegulatoryInstrument node and Native Structural Graph
attribute tables in `docs/architecture/ps-service-container-architecture.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

RegulatoryInstrumentStatus = Literal["active", "superseded", "vacated"]
SourceType = Literal["external", "internal"]
InstrumentType = Literal["regulation", "directive", "national_transposition"]


class RegulatoryInstrumentMetadata(BaseModel):
    """Bibliographic metadata for a RegulatoryInstrument node.

    Crosses the adapter to ingestion-core boundary, so it is a frozen Pydantic model that
    validates on construction.

    Attributes:
        title: Official title; non-empty.
        jurisdiction: Issuing jurisdiction; non-empty.
        effective_date: Date the instrument takes effect; an ISO-8601 string is parsed to a
            `date`, and a malformed one fails validation.
        version: Version label; non-empty.
        status: One of `active`, `superseded`, `vacated`.
        source_type: `external` or `internal`.
        instrument_type: `regulation`, `directive` or `national_transposition`. Required when
            `source_type` is `external`, forbidden when it is `internal`.
        celex: Source CELEX identifier in base-act form (for example `32024R2847`), or None when
            unknown or not applicable. Carried onto the RegulatoryInstrument node so the change
            monitor can poll Cellar for consolidated versions. No cross-field rule applies.
    """

    model_config = ConfigDict(frozen=True)
    title: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=1)
    # `date`, not `str`: Pydantic parses an ISO-8601 string into a real `date`, so a malformed
    # date fails at construction instead of passing through as an opaque string.
    effective_date: date
    version: str = Field(min_length=1)
    status: RegulatoryInstrumentStatus
    source_type: SourceType
    instrument_type: InstrumentType | None = None
    # Optional: a null `celex` is valid (the adapter may not know it, or the source is internal).
    celex: str | None = None

    @model_validator(mode="after")
    def _check_instrument_type_matches_source_type(self) -> Self:
        """Require `instrument_type` for external sources and forbid it for internal ones.

        Returns:
            The validated model.

        Raises:
            ValueError: `source_type` is `external` and `instrument_type` is None, or
                `source_type` is `internal` and `instrument_type` is set.
        """
        if self.source_type == "external" and self.instrument_type is None:
            raise ValueError("instrument_type is required when source_type is 'external'")
        if self.source_type == "internal" and self.instrument_type is not None:
            raise ValueError("instrument_type must be absent when source_type is 'internal'")
        return self


@dataclass(frozen=True, slots=True)
class StructuralNode:
    """One native structural element of an instrument (chapter, article, recital and so on).

    The shape is defined by the adapter, not a fixed project-wide schema, so it is a plain frozen
    dataclass.

    Attributes:
        element_type: Source vocabulary label, for example CHAPTER, SECTION, ARTICLE, PARAGRAPH,
            ANNEX or RECITAL.
        id: Node id, unique within the instrument.
        properties: Element properties (text, citation_ref, order, heading), each a str or int.
    """

    element_type: str  # Cellar/ELI vocabulary: CHAPTER/SECTION/ARTICLE/PARAGRAPH/ANNEX/RECITAL
    id: str
    properties: dict[str, str | int]  # text, citation_ref, order, heading (all str|int, no Any)


@dataclass(frozen=True, slots=True)
class StructuralEdge:
    """One parent to child edge in the native structural graph.

    Attributes:
        parent_element_type: `RegulatoryInstrument` for top-level children, otherwise the
            parent `StructuralNode.element_type`.
        parent_id: Id of the parent node.
        child_element_type: `element_type` of the child node.
        child_id: Id of the child node.
    """

    parent_element_type: (
        str  # "RegulatoryInstrument" for top-level children, else a StructuralNode.element_type
    )
    parent_id: str
    child_element_type: str
    child_id: str


@dataclass(frozen=True, slots=True)
class FetchedRegulatoryInstrumentStructure:
    """What an adapter returns for one instrument: fetch and parse are a single action.

    Attributes:
        metadata: The instrument's bibliographic metadata.
        nodes: Structural nodes held in memory, ready to be persisted.
        edges: Parent to child edges between the nodes (and the instrument node).
    """

    metadata: RegulatoryInstrumentMetadata
    nodes: tuple[StructuralNode, ...]
    edges: tuple[StructuralEdge, ...]


@dataclass(frozen=True, slots=True)
class ReachabilityCount:
    """Per-label result of `verify_structural_graph_reachable`.

    How many nodes of one label exist in the graph versus how many are reachable from the
    RegulatoryInstrument node.

    Attributes:
        total: Count of nodes with the label in the graph.
        reachable: Count of those nodes reachable from the RegulatoryInstrument node.
    """

    total: int
    reachable: int


@dataclass(frozen=True, slots=True)
class IngestResult:
    """The outcome of one `ingest_regulatory_instrument` run.

    Attributes:
        regulatory_instrument_id: Final `{SHORT}-{VERSION}` id of the persisted instrument.
        run_id: Identifier of this run.
        counts: Per-label `ReachabilityCount` measured after the write.
    """

    regulatory_instrument_id: str
    run_id: str
    counts: dict[str, ReachabilityCount]
