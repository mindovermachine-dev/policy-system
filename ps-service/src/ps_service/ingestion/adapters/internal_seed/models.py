"""Pydantic models for the internal-regulation intake format (D2/D7).

`InternalRegulationSeed` is the Pydantic-parsed form of a submitted document,
built only after `schema.validate_seed_document` has already passed (the
structural JSON Schema layer) -- these models are the second, statically-typed
half of the same boundary check, not a replacement for it. Per D2,
`InternalRegulationSeed` declares only `nodes`/`edges` and forbids any other
top-level key (`extra="forbid"`), so a submitted `graph_name` is rejected
structurally even if the packaged JSON Schema copy ever drifted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NodeLabel = Literal["RegulatoryInstrument", "Role", "Requirement", "Obligation", "Capability"]
"""The intake format's five allowed node labels -- mirrors the packaged JSON Schema's
`nodeLabel` enum exactly (D7)."""

EdgeType = Literal["DEFINES", "EXPRESSES", "HAS", "SATISFIED_BY", "REQUIRES"]
"""The intake format's five allowed edge types -- mirrors the packaged JSON Schema's
`edge.type` enum exactly (D7)."""


class SeedRef(BaseModel):
    """A `{label, id}` reference to a node declared elsewhere in the same document."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    label: NodeLabel
    id: str = Field(min_length=1)


class SeedNode(BaseModel):
    """One submitted node.

    `id` is the customer's own local id -- never stored verbatim except for
    `RegulatoryInstrument` (used as-is); every other label's canonical id is
    minted by the persister from `properties` content (`ps-domain-concepts.md`'s
    identity formulas), never from this field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    label: NodeLabel
    id: str = Field(min_length=1)
    properties: dict[str, str | float] = Field(default_factory=dict)


class SeedEdge(BaseModel):
    """One submitted edge, referencing its endpoints by `{label, id}` (`SeedRef`)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    type: EdgeType
    from_: SeedRef = Field(alias="from")
    to: SeedRef
    properties: dict[str, str] = Field(default_factory=dict)


class InternalRegulationSeed(BaseModel):
    """The whole submitted document: `{nodes: [...], edges: [...]}` (D2).

    No `graph_name` or other target-graph field exists on this model --
    PS Service determines where the data is stored from the submitted
    `RegulatoryInstrument` node's own `id` (`internal-regulation-intake-
    format.md`'s "Format overview"). `extra="forbid"` makes AC-BI-010's
    "graph_name can never redirect a write" guarantee structural: an
    unrecognized top-level key is rejected outright, not silently dropped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[SeedNode, ...]
    edges: tuple[SeedEdge, ...]
