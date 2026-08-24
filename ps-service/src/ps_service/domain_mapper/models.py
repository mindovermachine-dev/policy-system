"""ps_service.domain_mapper core types — the shapes `extraction.py`/
`derivation.py` build and consume internally, plus the two actions' return
values.

Per PLAN_REVIEWED.md §2: `ExtractionUnit`/`ExtractionResult`/
`DerivationResult` are plain frozen dataclasses — internal pipeline
plumbing, not PS Conceptual Model types crossing to an external caller.
`RequirementCandidate`/`ObligationAssignment`/`CapabilityDecision` are
Pydantic frozen models per L2 Data Modeling's instruction to use Pydantic
for LLM-structured-extraction outputs — each is validated straight from a
parsed LLM JSON response, so `Field` constraints double as the runtime
validation boundary (L1 Fail Fast at Boundaries), not just static typing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class ExtractionUnit:
    """One native ARTICLE or PARAGRAPH element to extract from.

    `citation_ref` is the exact string that becomes the `DEFINES`/
    `EXPRESSES` edges' `source_ref` (AC-001) — read verbatim from the
    native structural graph, never reconstructed.
    """

    citation_ref: str
    text: str
    article_number: str
    paragraph_number: str  # "1" for a paragraph-less Article
    article_heading: str


class RequirementCandidate(BaseModel):
    """Stage-1 LLM output, one per independent duty found in a unit.

    Not yet a graph node — `role_name` is a raw string pending
    canonicalization (`extraction.py::_canonicalize_roles`) into a real
    Role node id.
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


@dataclass(frozen=True, slots=True)
class RoleNode:
    """One canonicalized Role, ready for `graph_writer.persist_role_and_requirement_graph`
    to `MERGE`. `id` is `identity.role_id()`'s output; `properties` carries
    `name`/`confidence` (CA doc §0.1's Role attributes) — a plain properties
    dict, not a per-field Pydantic model, mirroring `ps_service.ingestion.
    models.StructuralNode`'s established shape for graph-write payloads in
    this codebase (PLAN_REVIEWED.md §5.4)."""

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class RoleDefinesEdge:
    """Regulation -[:DEFINES {source_ref}]-> Role. `role_node_id` is the
    target Role's `id`; `source_ref` is the first duty-bearing candidate's
    `unit_citation_ref` (PLAN_REVIEWED.md §5.2 step 4)."""

    role_node_id: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class RequirementNode:
    """One Requirement, ready for `graph_writer.persist_role_and_requirement_graph`
    to `MERGE`. `id` is `identity.requirement_id()`'s output, possibly
    disambiguated with a `#2`/`#3` suffix (PLAN_REVIEWED.md §5.2 step 5).
    `properties` carries `text`/`type`/`confidence` (CA doc §0.1's
    Requirement attributes) plus `role_id` — a plain bookkeeping property
    (NOT an Edge Catalog relationship) pointing at the owning Role node's
    `id`, written here and read back by `derivation.py` per §7.2."""

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class RequirementExpressesEdge:
    """Regulation -[:EXPRESSES {source_ref}]-> Requirement. `source_ref` is
    the candidate's own `unit_citation_ref` (AC-001)."""

    requirement_node_id: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """`extract_roles_and_requirements()`'s return value — the outcome of
    one `ExtractRolesAndRequirements` call."""

    regulation_id: str
    role_node_ids: dict[str, str]  # role_name -> role node id, for derivation's use
    requirement_ids: tuple[str, ...]
    candidate_count: int
    skipped_unit_count: int  # units whose LLM response was malformed — logged, not raised
    requirement_id_collisions: tuple[str, ...]
    # Disambiguated ids (base_id + "#2", "#3", ...) minted when two candidates
    # produced the same base requirement_id with DIFFERENT text
    # (PLAN_REVIEWED.md §5.2 step 5, B2 fix). Never raised as an exception —
    # surfaced here and via a corresponding outcome="collision" log entry.


@dataclass(frozen=True, slots=True)
class RoleRequirements:
    """One Role's ordered Requirements, as read back from the baseline
    graph via `Requirement.properties["role_id"]` (PLAN_REVIEWED.md §7.2).

    `derivation.py::_derive_obligations` (Increment 12) consumes this shape
    but does not itself read the graph — building it from a live baseline
    graph (a `_read_requirements_by_role` helper) is Increment 16's job
    (derivation orchestration; PLAN_REVIEWED.md §11 explicitly lists that
    helper under Increment 16, "Wires Increments 11-15 + §7.2's
    `_read_requirements_by_role`"). Increment 12's own tests construct this
    directly, in the document order the whole-run algorithm requires
    (§7.3: "iterating Roles in document order, and within each Role its
    Requirements in document order")."""

    role_node_id: str
    role_name: str
    requirements: tuple[tuple[str, str], ...]  # (requirement_id, requirement_text), document order


class ObligationAssignment(BaseModel):
    """Stage-2 LLM output — one per Requirement, the mint-or-match-or-
    unmatchable decision `derivation.py::_derive_obligations` makes for it
    (PLAN_REVIEWED.md §7.3/§7.5).

    `obligation_node_id`/`obligation_text` are `prompts.py::
    parse_obligation_response`'s own PROPOSED identity/text — for a match,
    `obligation_text` is the matched registry entry's text and
    `obligation_node_id` is `identity.obligation_id()` recomputed from it
    (always equal to the matched id, by construction, since the registry's
    own keys are themselves `obligation_id()` outputs); for a mint, both
    are freshly derived from `new_text`. This is a PROPOSAL only —
    `derivation.py`'s whole-run registry may still resolve a DIFFERENT,
    role-qualified final id/text on a genuine cross-Role collision
    (PLAN_REVIEWED.md §7.3's B1 fix) — `obligation_node_id` here is never
    assumed to be the final persisted id."""

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    role_node_id: str
    obligation_node_id: str | None  # None when unmatchable — AC-004
    obligation_text: str | None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class ObligationNode:
    """One Obligation, ready for a future `graph_writer.
    persist_obligation_and_capability_graph` (Increment 15, out of this
    batch's scope) to `MERGE`. `id` is `identity.obligation_id()`'s
    output — possibly role-qualified per PLAN_REVIEWED.md §7.3's
    collision-aware algorithm. `properties` carries `text`/`confidence`
    (CA doc's Obligation attributes; the Edge Catalog states Obligation
    carries no `source_ref` of its own — provenance is transitive via
    `SATISFIED_BY` -> `EXPRESSES`)."""

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class ObligationHasEdge:
    """Role -[:HAS]-> Obligation. No properties (Edge Catalog, §0.2) —
    exactly one per Obligation node by construction, since
    PLAN_REVIEWED.md §7.3's algorithm only ever creates this edge at the
    moment an Obligation id is first minted (globally, or as a
    role-qualified id on a collision), never again for the same id."""

    role_node_id: str
    obligation_node_id: str


@dataclass(frozen=True, slots=True)
class RequirementSatisfiedByEdge:
    """Requirement -[:SATISFIED_BY]-> Obligation. No properties (Edge
    Catalog, §0.2)."""

    requirement_id: str
    obligation_node_id: str


class CapabilityDecision(BaseModel):
    """Stage-3 LLM output — one per Capability a distinct Obligation
    requires (PLAN_REVIEWED.md §7.4)."""

    model_config = ConfigDict(frozen=True)

    obligation_node_id: str
    capability_node_id: str
    name: str
    description: str | None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class CapabilityNode:
    """One Capability, ready for a future `graph_writer.
    persist_obligation_and_capability_graph` (Increment 15, out of this
    batch's scope) to `MERGE`. `id` is `identity.capability_id()`'s
    output — content-derived from `name` alone (deliberately Obligation-
    and Role-independent, PLAN_REVIEWED.md §7.4), so identical names
    across distinct Obligations converge onto one shared node.
    `properties` carries `name`/`confidence` and, when set, `description`
    (CA doc's Capability attributes; the Edge Catalog states Capability
    carries no `source_ref` of its own)."""

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class CapabilityRequiresEdge:
    """Obligation -[:REQUIRES]-> Capability. No properties (Edge Catalog,
    §0.2). More than one such edge may share the same `capability_node_id`
    (Capability convergence across distinct Obligations, §7.4) or the same
    `obligation_node_id` (multi-capability-per-Obligation, also §7.4)."""

    obligation_node_id: str
    capability_node_id: str


@dataclass(frozen=True, slots=True)
class DerivationResult:
    """`derive_obligations_and_capabilities()`'s return value — the outcome
    of one `DeriveObligationsAndCapabilities` call."""

    regulation_id: str
    obligation_node_ids: tuple[str, ...]
    capability_node_ids: tuple[str, ...]
    unmatched_requirement_ids: tuple[str, ...]  # AC-004 — surfaced, never silently absent
