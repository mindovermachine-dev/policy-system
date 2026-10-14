"""Pydantic models for the internal-regulation intake format.

`InternalRegulationSeed` is the typed form of a submitted document, built after
`schema.validate_seed_document` has passed the structural JSON Schema layer. The models are the
second, statically typed half of the same boundary check, not a replacement for it.
`InternalRegulationSeed` declares only `nodes` and `edges` and forbids any other top-level key
(`extra="forbid"`), so a submitted `graph_name` is rejected structurally even if the packaged JSON
Schema were out of date.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NodeLabel = Literal[
    "RegulatoryInstrument",
    "Role",
    "Requirement",
    "Obligation",
    "Capability",
    "Policy",
    "Standard",
    "Control",
    "PracticeArea",
    "RiskPath",
]
"""The intake format's allowed node labels. Pinned to the code-defined domain
schema (and so to the generated intake JSON Schema's `nodeLabel` enum) by
`tests/ingestion/adapters/internal_seed/test_models_vocabulary_pin.py`; drift fails there.
`Policy`, `Standard` and `Control` are authored governance labels; `PracticeArea` and `RiskPath`
are authored baseline-classification labels (see the intake format doc's PracticeArea and
RiskPath sections)."""

EdgeType = Literal[
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
    "MITIGATED_BY",
]
"""The intake format's allowed edge types. Pinned to the code-defined domain
schema (and so to the generated intake JSON Schema's `edge.type` enum) by
`tests/ingestion/adapters/internal_seed/test_models_vocabulary_pin.py`; schema-only
system-minted edge types are named exceptions there. `GOVERNED_BY`, `SUPPORTED_BY` and
`IMPLEMENTED_BY` are authored governance edges."""


class SeedRef(BaseModel):
    """A `{label, id}` reference to a node declared elsewhere in the same document.

    Attributes:
        label: The referenced node's label; one of `NodeLabel`.
        id: The referenced node's local id as declared in the document; non-empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    label: NodeLabel
    id: str = Field(min_length=1)


class SeedNode(BaseModel):
    """One submitted node.

    The customer's local `id` is stored verbatim only for `RegulatoryInstrument`. For every other
    label the persister mints the canonical id from `properties` content (the identity formulas in
    `ps-domain-concepts.md`), never from this field.

    Attributes:
        label: The node's label; one of `NodeLabel`.
        id: The customer's local id, unique per label within the document; non-empty.
        properties: Node properties keyed by name; values are strings or floats. Empty by default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    label: NodeLabel
    id: str = Field(min_length=1)
    properties: dict[str, str | float] = Field(default_factory=dict)


class SeedEdge(BaseModel):
    """One submitted edge, referencing its endpoints by `{label, id}` (`SeedRef`).

    Attributes:
        type: The edge type; one of `EdgeType`.
        from_: The source endpoint; serialised and parsed under the key `from`.
        to: The target endpoint.
        properties: Edge properties keyed by name; values are strings. Empty by default.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    type: EdgeType
    from_: SeedRef = Field(alias="from")
    to: SeedRef
    properties: dict[str, str] = Field(default_factory=dict)


class InternalRegulationSeed(BaseModel):
    """The whole submitted document: `{nodes: [...], edges: [...]}`.

    The model has no `graph_name` or other target-graph field. PS Service derives where the data
    is stored from the submitted `RegulatoryInstrument` node's own `id` (see "Format overview" in
    `internal-regulation-intake-format.md`). `extra="forbid"` makes that structural: an
    unrecognized top-level key is rejected outright rather than silently dropped, so no submitted
    field can redirect a write.

    Attributes:
        nodes: Every submitted node; may be empty at this layer (content rules are checked by the
            persister).
        edges: Every submitted edge; each endpoint must reference a node in `nodes`, which the
            persister enforces.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[SeedNode, ...]
    edges: tuple[SeedEdge, ...]
