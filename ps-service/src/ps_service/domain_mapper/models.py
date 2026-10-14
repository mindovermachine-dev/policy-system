"""ps_service.domain_mapper core types.

The shapes `extraction.py` and `derivation.py` build and consume, plus the two actions' return
values.

`ExtractionUnit`, `ExtractionResult` and `DerivationResult` are plain frozen dataclasses:
internal pipeline plumbing, not conceptual-model types crossing to an external caller.
`RequirementCandidate`, `DefinedTermCandidate`, `ObligationAssignment`, `CapabilityDecision` and
`CapabilityReuseVerdict` are frozen Pydantic models, each validated straight from a parsed LLM
JSON response, so their `Field` constraints are the runtime validation boundary and not only
static typing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class ExtractionUnit:
    """One native ARTICLE, PARAGRAPH or ANNEX element the LLM extracts from.

    Attributes:
        citation_ref: Citation label read verbatim from the native structural graph, never
            reconstructed; it becomes the `source_ref` of the `DEFINES` and `EXPRESSES` edges.
        text: The element's text.
        article_number: Article number as a digit string (an Annex label for Annex units).
        paragraph_number: Paragraph number as a digit string; `"1"` for a paragraph-less
            Article or an Annex.
        article_heading: The Article's heading; empty for an Annex.
    """

    citation_ref: str
    text: str
    article_number: str
    paragraph_number: str  # "1" for a paragraph-less Article
    article_heading: str


class RequirementCandidate(BaseModel):
    """Stage-1 LLM output: one independent duty found in an extraction unit.

    Not yet a graph node: `role_name` is a raw string pending canonicalization
    (`extraction.py::_canonicalize_roles`) into a Role node id.

    Attributes:
        unit_citation_ref: `citation_ref` of the unit the duty came from.
        unit_article_number: Article number of that unit.
        unit_paragraph_number: Paragraph number of that unit.
        role_name: Name of the Role bearing the duty; non-empty.
        text: The duty statement; non-empty.
        type: Whether the duty is a requirement, a prohibition or a recommendation.
        letter_suffix: Sub-paragraph letter when the unit holds several duties, else `None`.
        confidence: LLM confidence in `[0.0, 1.0]`.
    """

    model_config = ConfigDict(frozen=True)

    unit_citation_ref: str
    unit_article_number: str
    unit_paragraph_number: str
    role_name: str = Field(min_length=1)
    text: str = Field(min_length=1)
    type: Literal["requirement", "prohibition", "recommendation"]
    letter_suffix: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class DefinedTermCandidate(BaseModel):
    """Stage-1b LLM output: one formally defined term found in a definitions unit.

    Mirrors `RequirementCandidate`'s validation contract. Only `term` comes from the LLM's JSON;
    `citation_ref` is filled at the parsing boundary (`prompts.py::parse_definitions_response`)
    from the source unit, never invented by the LLM.

    Attributes:
        term: The defined term; non-empty.
        citation_ref: The source unit's `citation_ref`, verbatim; non-empty.
    """

    model_config = ConfigDict(frozen=True)

    term: str = Field(min_length=1)
    citation_ref: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class RoleNode:
    """A canonicalized Role, submitted as a gateway node primitive by the graph writer.

    The plain properties dict mirrors `ps_service.ingestion.models.StructuralNode`'s shape for
    graph-write payloads.

    Attributes:
        id: The Role id, as produced by `identity.role_id()`.
        properties: Node properties `name` and `confidence`.
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class RoleDefinesEdge:
    """RegulatoryInstrument -[:DEFINES {source_ref}]-> Role.

    Attributes:
        role_node_id: The target Role's `id`.
        source_ref: Set by `extraction.py::_canonicalize_roles`. When the Role's name matches a
            formally defined term found by the definitions pass, the term's own `citation_ref`
            (the location of the defining text); otherwise the first duty-bearing candidate's
            `unit_citation_ref`.
    """

    role_node_id: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class RequirementNode:
    """A Requirement, submitted as a gateway node primitive by the graph writer.

    Attributes:
        id: The Requirement id from `identity.requirement_id()`, disambiguated with a
            `#<n>` counter suffix when two candidates share a base id but differ in text.
        properties: Node properties `text`, `type` and `confidence`, plus `role_id`: a plain
            bookkeeping property (not an Edge Catalog relationship) holding the owning Role's
            `id`, written here and read back by `derivation.py`.
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class RequirementExpressesEdge:
    """RegulatoryInstrument -[:EXPRESSES {source_ref}]-> Requirement.

    Attributes:
        requirement_node_id: The Requirement's `id`.
        source_ref: The candidate's own `unit_citation_ref`.
    """

    requirement_node_id: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """The outcome of one `extract_roles_and_requirements()` call.

    Attributes:
        regulatory_instrument_id: The instrument the call extracted from.
        role_node_ids: Mapping of Role name to Role node id, for derivation's use.
        requirement_ids: Ids of the Requirements written, in document order; empty when none.
        candidate_count: Number of Requirement candidates the LLM returned across all units.
        skipped_unit_count: Units whose LLM response was malformed; they are logged, not raised.
        requirement_id_collisions: Disambiguated ids minted when two candidates produced the
            same base id with different text. Surfaced here and as an `outcome="collision"` log
            entry, never raised.
    """

    regulatory_instrument_id: str
    role_node_ids: dict[str, str]  # role_name -> role node id, for derivation's use
    requirement_ids: tuple[str, ...]
    candidate_count: int
    skipped_unit_count: int  # units whose LLM response was malformed — logged, not raised
    requirement_id_collisions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoleRequirements:
    """One Role's ordered Requirements, as read back from the baseline graph.

    Requirements are grouped by their `role_id` property. `derivation.py::_derive_obligations`
    consumes this shape without reading the graph itself, and expects Roles in document order
    and, within each Role, Requirements in document order.

    Attributes:
        role_node_id: The Role's `id`.
        role_name: The Role's name.
        requirements: `(requirement_id, requirement_text)` pairs in document order.
    """

    role_node_id: str
    role_name: str
    requirements: tuple[tuple[str, str], ...]  # (requirement_id, requirement_text), document order


class ObligationAssignment(BaseModel):
    """Stage-2 LLM output: a mint, match or unmatchable decision for one Requirement.

    Produced by `prompts.py::parse_obligation_response` for
    `derivation.py::_derive_obligations`. The `obligation_*` fields are a proposal only:
    `derivation.py`'s whole-run registry resolves the final id (a mint, or a same-Role reuse),
    so `obligation_node_id` is not necessarily the persisted id. For a match,
    `obligation_text` is the matched registry entry's text and `obligation_node_id` is
    `identity.obligation_id(role_node_id, text)` recomputed from it. For a mint, both are
    derived from the new text.

    Attributes:
        requirement_id: The Requirement being decided.
        role_node_id: Id of the Role that bears the Requirement.
        obligation_node_id: Proposed Obligation id; `None` when the Requirement is unmatchable.
        obligation_text: Proposed Obligation text; `None` when unmatchable.
        confidence: LLM confidence in `[0.0, 1.0]`.
    """

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    role_node_id: str
    obligation_node_id: str | None
    obligation_text: str | None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class ObligationNode:
    """One Obligation, submitted as a gateway node primitive by the graph writer.

    An Obligation carries no `source_ref` of its own; provenance is transitive via
    `SATISFIED_BY` then `EXPRESSES`.

    Attributes:
        id: The Obligation id from `identity.obligation_id(role_node_id, text)`; Role-scoped, so
            the Obligation belongs to exactly one Role.
        properties: Node properties `text` and `confidence`.
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class ObligationHasEdge:
    """Role -[:HAS]-> Obligation.

    The edge has no properties. There is exactly one per Obligation node: the Obligation id is
    Role-scoped, and the edge is created only when the id is first minted.

    Attributes:
        role_node_id: The bearing Role's `id`.
        obligation_node_id: The Obligation's `id`.
    """

    role_node_id: str
    obligation_node_id: str


@dataclass(frozen=True, slots=True)
class RequirementSatisfiedByEdge:
    """Requirement -[:SATISFIED_BY]-> Obligation.

    The edge has no properties.

    Attributes:
        requirement_id: The Requirement's `id`.
        obligation_node_id: The satisfying Obligation's `id`.
    """

    requirement_id: str
    obligation_node_id: str


class CapabilityDecision(BaseModel):
    """Stage-3 LLM output: one Capability that a distinct Obligation requires.

    Attributes:
        obligation_node_id: The Obligation that requires the Capability.
        capability_node_id: Id of the Capability to attach.
        name: The Capability name.
        description: The Capability description, or `None` when absent.
        confidence: LLM confidence in `[0.0, 1.0]`.
        is_reuse: True when the decision attaches the Obligation to a Capability already in the
            whole-run registry (a matched existing item, or a mint whose `capability_node_id` is
            already registered), so it is verified before it is applied.
    """

    model_config = ConfigDict(frozen=True)

    obligation_node_id: str
    capability_node_id: str
    name: str
    description: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    is_reuse: bool = False


class CapabilityReuseVerdict(BaseModel):
    """Reuse-verification LLM output: one verdict per Capability reuse proposal.

    `parse_capability_reuse_verdict` enforces that a `reject` carries the replacement fields.

    Attributes:
        verdict: `accept` keeps the proposed existing Capability; `reject` mints a more
            specific one instead.
        new_name: Name of the Capability to mint; set on `reject`.
        new_description: Description of the Capability to mint; set on `reject`.
        confidence: Confidence in `[0.0, 1.0]` for the Capability to mint; set on `reject`.
    """

    model_config = ConfigDict(frozen=True)

    verdict: Literal["accept", "reject"]
    new_name: str | None = None
    new_description: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class CapabilityNode:
    """One Capability, submitted as a gateway node primitive by the graph writer.

    A Capability carries no `source_ref` of its own.

    Attributes:
        id: The Capability id from `identity.capability_id()`, derived from `name` alone and
            independent of Obligation and Role, so identical names across distinct Obligations
            converge onto one shared node.
        properties: Node properties `name` and `confidence`, plus `description` when set.
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class CapabilityRequiresEdge:
    """Obligation -[:REQUIRES]-> Capability.

    The edge has no properties. More than one edge may share a `capability_node_id` (Capability
    convergence across Obligations) or an `obligation_node_id` (several Capabilities per
    Obligation).

    Attributes:
        obligation_node_id: The requiring Obligation's `id`.
        capability_node_id: The required Capability's `id`.
    """

    obligation_node_id: str
    capability_node_id: str


@dataclass(frozen=True, slots=True)
class DerivationResult:
    """The outcome of one `derive_obligations_and_capabilities()` call.

    Attributes:
        regulatory_instrument_id: The instrument the call derived for.
        obligation_node_ids: Ids of the Obligations written.
        capability_node_ids: Ids of the Capabilities written.
        unmatched_requirement_ids: Requirements the LLM judged unmatchable; surfaced, never
            silently absent.
        unmatched_obligation_ids: Obligations whose Capability derivation response was
            malformed; surfaced, never silently dropped.
    """

    regulatory_instrument_id: str
    obligation_node_ids: tuple[str, ...]
    capability_node_ids: tuple[str, ...]
    unmatched_requirement_ids: tuple[str, ...]
    unmatched_obligation_ids: tuple[str, ...]
