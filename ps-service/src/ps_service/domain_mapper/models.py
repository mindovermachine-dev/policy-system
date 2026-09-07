"""ps_service.domain_mapper core types.

The shapes `extraction.py`/`derivation.py` build and consume internally,
plus the two actions' return values.

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
    """A canonicalized Role for `graph_writer.persist_role_and_requirement_graph` to `MERGE`.

    `id` is `identity.role_id()`'s output; `properties` carries
    `name`/`confidence` (CA doc §0.1's Role attributes) — a plain
    properties dict, not a per-field Pydantic model, mirroring
    `ps_service.ingestion.models.StructuralNode`'s established shape for
    graph-write payloads in this codebase (PLAN_REVIEWED.md §5.4).
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class RoleDefinesEdge:
    """RegulatoryInstrument -[:DEFINES {source_ref}]-> Role.

    `role_node_id` is the target Role's `id`; `source_ref` is the first
    duty-bearing candidate's `unit_citation_ref` (PLAN_REVIEWED.md §5.2
    step 4).
    """

    role_node_id: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class RequirementNode:
    """A Requirement for `graph_writer.persist_role_and_requirement_graph` to `MERGE`.

    `id` is `identity.requirement_id()`'s output, possibly disambiguated
    with a `#2`/`#3` suffix (PLAN_REVIEWED.md §5.2 step 5). `properties`
    carries `text`/`type`/`confidence` (CA doc §0.1's Requirement
    attributes) plus `role_id` — a plain bookkeeping property (NOT an Edge
    Catalog relationship) pointing at the owning Role node's `id`, written
    here and read back by `derivation.py` per §7.2.
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class RequirementExpressesEdge:
    """RegulatoryInstrument -[:EXPRESSES {source_ref}]-> Requirement.

    `source_ref` is the candidate's own `unit_citation_ref` (AC-001).
    """

    requirement_node_id: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """`extract_roles_and_requirements()`'s return value.

    The outcome of one `ExtractRolesAndRequirements` call.
    """

    regulatory_instrument_id: str
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
    """One Role's ordered Requirements, as read back from the baseline graph.

    Via `Requirement.properties["role_id"]` (PLAN_REVIEWED.md §7.2).

    `derivation.py::_derive_obligations` (Increment 12) consumes this shape
    but does not itself read the graph — building it from a live baseline
    graph (a `_read_requirements_by_role` helper) is Increment 16's job
    (derivation orchestration; PLAN_REVIEWED.md §11 explicitly lists that
    helper under Increment 16, "Wires Increments 11-15 + §7.2's
    `_read_requirements_by_role`"). Increment 12's own tests construct this
    directly, in the document order the whole-run algorithm requires
    (§7.3: "iterating Roles in document order, and within each Role its
    Requirements in document order").
    """

    role_node_id: str
    role_name: str
    requirements: tuple[tuple[str, str], ...]  # (requirement_id, requirement_text), document order


class ObligationAssignment(BaseModel):
    """Stage-2 LLM output — one mint-or-match-or-unmatchable decision per Requirement.

    Made by `derivation.py::_derive_obligations` (PLAN_REVIEWED.md
    §7.3/§7.5).

    `obligation_node_id`/`obligation_text` are `prompts.py::
    parse_obligation_response`'s own PROPOSED identity/text — for a match,
    `obligation_text` is the matched registry entry's text and
    `obligation_node_id` is `identity.obligation_id(role_node_id, text)`
    recomputed from it (always equal to the matched id, by construction,
    since the registry's own keys are themselves `obligation_id()` outputs
    for this Role); for a mint, both are freshly derived from `new_text`.
    This is a PROPOSAL only — `derivation.py`'s whole-run registry resolves
    the final id (a mint, or a same-Role reuse); `obligation_node_id` here
    is not assumed to be the final persisted id.
    """

    model_config = ConfigDict(frozen=True)

    requirement_id: str
    role_node_id: str
    obligation_node_id: str | None  # None when unmatchable — AC-004
    obligation_text: str | None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class ObligationNode:
    """One Obligation for a future `persist_obligation_and_capability_graph` `MERGE`.

    In `graph_writer` (Increment 15, out of this batch's scope). `id` is
    `identity.obligation_id(role_node_id, text)`'s output — Role-scoped
    (#42), so this Obligation belongs to exactly one Role. `properties`
    carries `text`/`confidence` (CA doc's Obligation attributes; the Edge
    Catalog states Obligation carries no `source_ref` of its own —
    provenance is transitive via `SATISFIED_BY` -> `EXPRESSES`).
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class ObligationHasEdge:
    """Role -[:HAS]-> Obligation.

    No properties (Edge Catalog, §0.2) — exactly one per Obligation node,
    structurally: the Obligation id is Role-scoped (#42), and this edge is
    created only at the moment an Obligation id is first minted, never
    again for the same id.
    """

    role_node_id: str
    obligation_node_id: str


@dataclass(frozen=True, slots=True)
class RequirementSatisfiedByEdge:
    """Requirement -[:SATISFIED_BY]-> Obligation.

    No properties (Edge Catalog, §0.2).
    """

    requirement_id: str
    obligation_node_id: str


class CapabilityDecision(BaseModel):
    """Stage-3 LLM output — one per Capability a distinct Obligation requires.

    PLAN_REVIEWED.md §7.4.
    """

    model_config = ConfigDict(frozen=True)

    obligation_node_id: str
    capability_node_id: str
    name: str
    description: str | None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class CapabilityNode:
    """One Capability for a future `persist_obligation_and_capability_graph` `MERGE`.

    In `graph_writer` (Increment 15, out of this batch's scope). `id` is
    `identity.capability_id()`'s output — content-derived from `name`
    alone (deliberately Obligation- and Role-independent, PLAN_REVIEWED.md
    §7.4), so identical names across distinct Obligations converge onto one
    shared node. `properties` carries `name`/`confidence` and, when set,
    `description` (CA doc's Capability attributes; the Edge Catalog states
    Capability carries no `source_ref` of its own).
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class CapabilityRequiresEdge:
    """Obligation -[:REQUIRES]-> Capability.

    No properties (Edge Catalog, §0.2). More than one such edge may share
    the same `capability_node_id` (Capability convergence across distinct
    Obligations, §7.4) or the same `obligation_node_id`
    (multi-capability-per-Obligation, also §7.4).
    """

    obligation_node_id: str
    capability_node_id: str


@dataclass(frozen=True, slots=True)
class DerivationResult:
    """`derive_obligations_and_capabilities()`'s return value.

    The outcome of one `DeriveObligationsAndCapabilities` call.
    """

    regulatory_instrument_id: str
    obligation_node_ids: tuple[str, ...]
    capability_node_ids: tuple[str, ...]
    unmatched_requirement_ids: tuple[str, ...]  # AC-004 — surfaced, never silently absent
    # AC-BI-002 (issue #64) — an Obligation whose Capability derivation response was malformed,
    # surfaced, never silently dropped
    unmatched_obligation_ids: tuple[str, ...]


# --- DeriveGovernanceArtifacts (issue #54, S3) ------------------------------
#
# Policy/Standard/Control derivation, internal-source only (D1: this action
# reads `{short}_baseline` directly, the same way `derive_obligations_and_
# capabilities` reads Requirements back, with no separate adapter object).
# `PolicyAssignment` mirrors `ObligationAssignment`'s mint/match/unmatchable
# shape (AC-BI-014 needs a "cannot derive a coherent Policy" outcome, exactly
# like Obligation derivation's AC-004); `StandardDecision`/`ControlDecision`
# mirror `CapabilityDecision`'s plain per-item shape (Standard/Control are
# weak entities, minted once per Policy/Standard within a run — no
# mint-or-match registry is needed for them).


class PolicyAssignment(BaseModel):
    """Stage-1 governance LLM output — one mint-or-match-or-unmatchable decision per Capability.

    Made by `governance.py`'s whole-run Policy derivation (mirrors
    `ObligationAssignment`'s own shape and role in `derivation.py`).

    `policy_node_id`/`policy_title` are `prompts.py::parse_policy_response`'s
    own PROPOSED identity/title — for a match, `policy_title` is the matched
    registry entry's title and `policy_node_id` is
    `identity.policy_id(title)` recomputed from it (always equal to the
    matched id, by construction, since the registry's own keys are
    themselves `policy_id()` outputs); for a mint, both are freshly derived
    from `new_title`. This is a PROPOSAL only — `governance.py`'s whole-run
    registry resolves the final id (a mint, or a run-wide reuse).
    """

    model_config = ConfigDict(frozen=True)

    capability_node_id: str
    policy_node_id: str | None  # None when unmatchable — AC-BI-014
    policy_title: str | None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class PolicyNode:
    """One Policy for a future `persist_governance_graph` `MERGE`.

    `id` is `identity.policy_id()`'s output — content-derived from `title`
    alone (`ps-domain-concepts.md`'s Policy identity note: deliberately not
    derived from any one governing Capability), so identical/semantically-
    matched titles across distinct Capabilities converge onto one shared
    node within a run. `properties` carries `title`/`status`/`confidence`
    (`status` always `"draft"` on mint — the Policy lifecycle's starting
    state; CA doc's `draft -> approved -> deprecated` workflow).
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class PolicyGovernedByEdge:
    """Capability -[:GOVERNED_BY]-> Policy.

    No properties (Edge Catalog, `ps-domain-concepts.md`). More than one
    such edge may share the same `policy_node_id` (Policy convergence across
    distinct Capabilities, mirroring `CapabilityRequiresEdge`'s own
    multi-source-per-target shape).
    """

    capability_node_id: str
    policy_node_id: str


class StandardDecision(BaseModel):
    """Stage-2 governance LLM output — one Standard per distinct (newly resolved) Policy.

    Made by `governance.py`'s Standard derivation, called once per distinct
    Policy processed in this run (a Standard is a weak entity of exactly one
    Policy — no mint-or-match registry, unlike Policy's own derivation).
    """

    model_config = ConfigDict(frozen=True)

    policy_node_id: str
    title: str
    description: str | None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class StandardNode:
    """One Standard for a future `persist_governance_graph` `MERGE`.

    `id` is `identity.standard_id()`'s output — a weak-entity id composed
    from the Policy it supports plus version (never a canonical hash, per
    `ps-domain-concepts.md`: a Standard exists only in the context of
    exactly one Policy). `properties` carries `title`/`implementation_status`/
    `confidence` and, when set, `description` (`implementation_status`
    always `"draft"` on mint — CA doc's `draft -> implemented -> reviewed ->
    deprecated` workflow).
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class PolicySupportedByEdge:
    """Policy -[:SUPPORTED_BY]-> Standard.

    No properties (Edge Catalog, `ps-domain-concepts.md`).
    """

    policy_node_id: str
    standard_node_id: str


class ControlDecision(BaseModel):
    """Stage-3 governance LLM output — one Control per distinct Standard.

    Made by `governance.py`'s Control derivation, called once per Standard
    produced in this run (a Control is a weak entity of exactly one
    Standard — no mint-or-match registry, mirroring Standard's own
    derivation).
    """

    model_config = ConfigDict(frozen=True)

    standard_node_id: str
    type: Literal["automated", "manual"]
    title: str
    description: str | None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class ControlNode:
    """One Control for a future `persist_governance_graph` `MERGE`.

    `id` is `identity.control_id()`'s output — a weak-entity id composed
    from the Standard it verifies plus control type. `properties` carries
    `type`/`title`/`implementation_status`/`confidence` and, when set,
    `description` (`implementation_status` always `"planned"` on mint — CA
    doc's `planned -> implemented -> reviewed -> deprecated` workflow).

    AC-BI-017: the four operational fields (`execution_frequency`,
    `last_test_date`, `next_review_date`, `evidence_ref`) are NEVER included
    in `properties` at mint time — omitted entirely (never written as a
    literal `null`), the same "never write `None`" convention
    `RequirementNode`'s `role_id` bookkeeping property already established
    (D6). They stay unset in the graph until an engineering team fills them
    in during actual implementation/testing.
    """

    id: str
    properties: dict[str, str | float]


@dataclass(frozen=True, slots=True)
class StandardImplementedByEdge:
    """Standard -[:IMPLEMENTED_BY]-> Control.

    No properties (Edge Catalog, `ps-domain-concepts.md`).
    """

    standard_node_id: str
    control_node_id: str


@dataclass(frozen=True, slots=True)
class GovernanceDerivationResult:
    """`derive_governance_artifacts()`'s return value.

    The outcome of one `DeriveGovernanceArtifacts` call.
    """

    regulatory_instrument_id: str
    policy_node_ids: tuple[str, ...]
    standard_node_ids: tuple[str, ...]
    control_node_ids: tuple[str, ...]
    # AC-BI-014 — a Capability whose Policy derivation could not resolve to a
    # coherent Policy (unmatchable, or a malformed/unparseable LLM response),
    # surfaced, never silently skipped.
    unmatched_capability_ids: tuple[str, ...]
