"""Referential validation, canonical-id minting, and dual-graph persistence (S2).

`ingest_internal_regulatory_instrument` is the internal-seed pipeline's single
stage (`PLAN.md` S2): given an already-parsed `InternalRegulationSeed`
(`adapter.read_seed`'s output), it

1. validates the whole document's referential integrity and cardinality
   rules -- every edge endpoint must be declared, every `Obligation` has
   exactly one bearing `Role`, every `Requirement` has exactly one
   `EXPRESSES` source -- entirely before any write (AC-BI-011, mirroring
   `ingestion/graph_writer.py`'s own B1-fix validate-then-write shape: this
   function raises with zero writes recorded, or writes everything);
2. mints every non-`RegulatoryInstrument` node's canonical id using the
   same identity formulas the external path's `domain_mapper.identity`
   module already exports (`role_id`/`obligation_id`/`capability_id` --
   imported, never reimplemented, so an internal Capability genuinely
   converges onto the same canonical node an external regulation's
   Capability of the same name would, per `ps-domain-concepts.md`'s
   cross-source convergence design) plus a `Requirement`-only formula local
   to this module (the external path's own `requirement_id` is shaped for a
   structured article/paragraph/letter locator, which does not fit an
   internal source's freeform `source_ref`);
3. writes the customer's raw, un-minted submission verbatim to
   `{short}_native` (B5) and the minted, remapped spine to `{short}_baseline`,
   both add/merge-only (AC-BI-012).

Both writes go through the Graph Write Gateway (issue #208): each graph is one mutation group
(`build_native_primitives`, `build_baseline_primitives`), nodes before edges, the native group
submitted first. Every group is built, and so validated, before either is submitted: a property
the gateway cannot store raises `InternalSeedError` with nothing written. An edge whose endpoint
is not among the group's nodes is dropped without a trace (AC-BI-004); this module opens no
graph connection of its own.

D6: `Requirement.properties["role_id"]` is synthesized here by resolving the
`SATISFIED_BY -> Obligation <- HAS -> Role` chain; when it resolves to more
than one distinct Role (or none), the property key is omitted entirely --
never set to `None`/`null`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ps_service.graph_gateway.models import (
    ExpectedPosition,
    MutationGroup,
    NodeRef,
    UpsertEdge,
    UpsertNode,
)
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.reconcile import (
    GroupPlan,
    edge_identity,
    prepare_group,
)
from ps_service.ingestion.graph_submission import (
    stamp_legacy_edges_before_first_write,
    submit_or_raise,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.graph_gateway.models import Primitive
    from ps_service.ingestion.adapters.internal_seed.models import (
        EdgeType,
        InternalRegulationSeed,
        NodeLabel,
        SeedEdge,
        SeedNode,
        SeedRef,
    )
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.logging import LogEmitter

_DEFAULT_CONFIDENCE = 1.0

_EDGE_ENDPOINT_LABELS: dict[EdgeType, tuple[NodeLabel, NodeLabel]] = {
    "DEFINES": ("RegulatoryInstrument", "Role"),
    "EXPRESSES": ("RegulatoryInstrument", "Requirement"),
    "HAS": ("Role", "Obligation"),
    "SATISFIED_BY": ("Requirement", "Obligation"),
    "REQUIRES": ("Obligation", "Capability"),
    "GOVERNED_BY": ("Capability", "Policy"),
    "SUPPORTED_BY": ("Policy", "Standard"),
    "IMPLEMENTED_BY": ("Standard", "Control"),
    "VERIFIED_BY": ("RiskPath", "Control"),
    "COVERS": ("PracticeArea", "Capability"),
    "OWNS": ("PracticeArea", "Policy"),
    "MITIGATED_BY": ("RiskPath", "Capability"),
}


@dataclass(frozen=True, slots=True)
class InternalIngestResult:
    """`ingest_internal_regulatory_instrument`'s return value -- one run's outcome."""

    regulatory_instrument_id: str
    role_count: int
    requirement_count: int
    obligation_count: int
    capability_count: int
    policy_count: int
    standard_count: int
    control_count: int
    practice_area_count: int
    risk_path_count: int


# --- Requirement id (internal-source formula; not domain_mapper.identity's external one) ---


def _slug(text: str) -> str:
    """Slugify `text`: lowercase, non-alphanumeric runs to `_`, no leading/trailing `_`.

    A small, deliberately duplicated copy of `domain_mapper.identity._slug`
    (that helper is module-private) -- per L1 DRY's "prefer duplication over
    the wrong abstraction," reaching into another component's private
    helper would be a worse coupling than re-deriving two lines locally.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return re.sub(r"_+", "_", slug)


def _requirement_id(regulatory_instrument_id: str, source_ref: str) -> str:
    """`{instrument_id}_req_{slug(source_ref)}` -- internal-source Requirement identity.

    Per `internal-regulation-intake-format.md`'s Ids table: "Minted from your
    instrument's id plus this Requirement's EXPRESSES edge's source_ref."
    Deliberately non-opaque, mirroring `domain_mapper.identity.requirement_id`'s
    own "location alone, no hash" design for the external path -- but that
    function's signature is shaped for a structured article/paragraph/letter
    locator, which an internal source's freeform `source_ref` does not fit,
    so this is its own formula, not a reuse.
    """
    return f"{regulatory_instrument_id}_req_{_slug(source_ref)}"


# --- fail-closed validation (AC-BI-011), entirely before any graph.query() call ---


@dataclass(frozen=True, slots=True)
class _SeedIndex:
    """Everything minting/persistence needs, pre-resolved by the validation pass."""

    regulatory_instrument: SeedNode
    obligation_bearer: dict[str, str]
    """Obligation local id -> the local id of its single bearing Role."""
    requirement_source_ref: dict[str, str]
    """Requirement local id -> its single EXPRESSES edge's source_ref."""
    requirement_obligation_ids: dict[str, tuple[str, ...]]
    """Requirement local id -> the local ids of the Obligations it SATISFIED_BY-links to."""
    policy_governor: dict[str, str]
    """Capability local id -> its single governing Policy's local id (GH #76 Slice 1).

    At most one outbound GOVERNED_BY per Capability -- zero is valid
    (governance is optional per-Capability, Design Decision 4)."""
    standard_supporter: dict[str, str]
    """Standard local id -> its single supporting Policy's local id (GH #76 Slice 2).

    Exactly one inbound SUPPORTED_BY per Standard -- zero or more than one
    is rejected (unlike GOVERNED_BY, a Standard is a weak entity of exactly
    one Policy)."""
    control_implementer: dict[str, str]
    """Control local id -> its single implemented Standard's local id (GH #76 Slice 3).

    Exactly one inbound IMPLEMENTED_BY per Control -- zero or more than one
    is rejected, mirroring standard_supporter's exactly-one-parent shape (a
    Control is a weak entity of exactly one Standard)."""


def find_regulatory_instrument(seed: InternalRegulationSeed) -> SeedNode:
    """Return the seed's single `RegulatoryInstrument` node.

    Called both by orchestration (to derive the `{short}_baseline`/
    `{short}_native` graph name before any FalkorDB handle is opened) and,
    again, by this module's own `_validate_and_index` -- deliberately
    re-checked rather than cached across that boundary, since orchestration
    and persistence are separate call sites that must each hold independently.

    Raises:
        InternalSeedError: The seed declares zero or more than one
            `RegulatoryInstrument` node.
    """
    instruments = [node for node in seed.nodes if node.label == "RegulatoryInstrument"]
    if len(instruments) != 1:
        raise InternalSeedError(
            "seed document must declare exactly one RegulatoryInstrument node, "
            f"found {len(instruments)}"
        )
    return instruments[0]


def _check_referential_integrity(seed: InternalRegulationSeed) -> None:
    """Every edge endpoint (and its label) must match a declared node, exactly."""
    declared = {(node.label, node.id) for node in seed.nodes}
    if len(declared) != len(seed.nodes):
        raise InternalSeedError("seed document declares a duplicate (label, id) node pair")
    for edge in seed.edges:
        _check_endpoint_declared(edge, edge.from_, "from", declared)
        _check_endpoint_declared(edge, edge.to, "to", declared)
        expected_from, expected_to = _EDGE_ENDPOINT_LABELS[edge.type]
        if edge.from_.label != expected_from or edge.to.label != expected_to:
            raise InternalSeedError(
                f"{edge.type} edge must go {expected_from} -> {expected_to}, "
                f"got {edge.from_.label} -> {edge.to.label}"
            )


def _check_endpoint_declared(
    edge: SeedEdge, ref: SeedRef, side: str, declared: set[tuple[str, str]]
) -> None:
    if (ref.label, ref.id) not in declared:
        raise InternalSeedError(
            f"{edge.type} edge references undeclared node {ref.label}:{ref.id!r} ({side})"
        )


def _index_obligation_bearers(seed: InternalRegulationSeed) -> dict[str, str]:
    """Obligation local id -> its single bearing Role's local id.

    Raises `InternalSeedError` for any Obligation with zero or more than one
    inbound HAS edge (the intake format's own "exactly one Role per
    Obligation" authoring rule).
    """
    bearer_by_obligation: dict[str, str] = {}
    has_count: dict[str, int] = {}
    for edge in seed.edges:
        if edge.type != "HAS":
            continue
        has_count[edge.to.id] = has_count.get(edge.to.id, 0) + 1
        bearer_by_obligation[edge.to.id] = edge.from_.id
    for node in seed.nodes:
        if node.label != "Obligation":
            continue
        count = has_count.get(node.id, 0)
        if count != 1:
            raise InternalSeedError(
                f"Obligation {node.id!r} must have exactly one inbound HAS edge, found {count}"
            )
    return bearer_by_obligation


def _index_requirement_source_refs(seed: InternalRegulationSeed) -> dict[str, str]:
    """Requirement local id -> its single EXPRESSES edge's `source_ref`.

    Raises `InternalSeedError` for any Requirement with zero or more than one
    inbound EXPRESSES edge, or an EXPRESSES edge missing `source_ref`.
    """
    source_ref_by_requirement: dict[str, str] = {}
    expresses_count: dict[str, int] = {}
    for edge in seed.edges:
        if edge.type != "EXPRESSES":
            continue
        expresses_count[edge.to.id] = expresses_count.get(edge.to.id, 0) + 1
        source_ref = edge.properties.get("source_ref")
        if not source_ref:
            raise InternalSeedError(
                f"EXPRESSES edge to Requirement {edge.to.id!r} is missing source_ref"
            )
        source_ref_by_requirement[edge.to.id] = source_ref
    for node in seed.nodes:
        if node.label != "Requirement":
            continue
        count = expresses_count.get(node.id, 0)
        if count != 1:
            raise InternalSeedError(
                f"Requirement {node.id!r} must have exactly one inbound "
                f"EXPRESSES edge, found {count}"
            )
    return source_ref_by_requirement


def _index_policy_governors(seed: InternalRegulationSeed) -> dict[str, str]:
    """Capability local id -> its single governing Policy's local id (GH #76 Slice 1).

    Raises `InternalSeedError`, naming the offending Capability, for any
    Capability with 2+ outbound GOVERNED_BY edges. Zero is valid --
    governance is optional per-Capability (Design Decision 4) -- so a
    Capability absent from the returned mapping simply has no Policy.
    """
    governor_by_capability: dict[str, str] = {}
    governed_by_count: dict[str, int] = {}
    for edge in seed.edges:
        if edge.type != "GOVERNED_BY":
            continue
        governed_by_count[edge.from_.id] = governed_by_count.get(edge.from_.id, 0) + 1
        governor_by_capability[edge.from_.id] = edge.to.id
    for capability_id_, count in governed_by_count.items():
        if count > 1:
            raise InternalSeedError(
                f"Capability {capability_id_!r} must have at most one outbound "
                f"GOVERNED_BY edge, found {count}"
            )
    return governor_by_capability


def _index_standard_supporters(seed: InternalRegulationSeed) -> dict[str, str]:
    """Standard local id -> its single supporting Policy's local id (GH #76 Slice 2).

    Raises `InternalSeedError` for any Standard with zero or more than one
    inbound SUPPORTED_BY edge -- mirrors `_index_obligation_bearers`'s
    exactly-one-bearer shape (a Standard is a weak entity of exactly one
    Policy, unlike GOVERNED_BY's optional-per-Capability rule).
    """
    supporter_by_standard: dict[str, str] = {}
    supported_by_count: dict[str, int] = {}
    for edge in seed.edges:
        if edge.type != "SUPPORTED_BY":
            continue
        supported_by_count[edge.to.id] = supported_by_count.get(edge.to.id, 0) + 1
        supporter_by_standard[edge.to.id] = edge.from_.id
    for node in seed.nodes:
        if node.label != "Standard":
            continue
        count = supported_by_count.get(node.id, 0)
        if count != 1:
            raise InternalSeedError(
                f"Standard {node.id!r} must have exactly one inbound "
                f"SUPPORTED_BY edge, found {count}"
            )
    return supporter_by_standard


def _index_control_implementers(seed: InternalRegulationSeed) -> dict[str, str]:
    """Control local id -> its single implemented Standard's local id (GH #76 Slice 3).

    Raises `InternalSeedError` for any Control with zero or more than one
    inbound IMPLEMENTED_BY edge -- mirrors `_index_standard_supporters`'s
    exactly-one-parent shape (a Control is a weak entity of exactly one
    Standard).
    """
    implementer_by_control: dict[str, str] = {}
    implemented_by_count: dict[str, int] = {}
    for edge in seed.edges:
        if edge.type != "IMPLEMENTED_BY":
            continue
        implemented_by_count[edge.to.id] = implemented_by_count.get(edge.to.id, 0) + 1
        implementer_by_control[edge.to.id] = edge.from_.id
    for node in seed.nodes:
        if node.label != "Control":
            continue
        count = implemented_by_count.get(node.id, 0)
        if count != 1:
            raise InternalSeedError(
                f"Control {node.id!r} must have exactly one inbound "
                f"IMPLEMENTED_BY edge, found {count}"
            )
    return implementer_by_control


def _policy_status_by_local_id(seed: InternalRegulationSeed) -> dict[str, str]:
    """Policy local id -> its own `status` property.

    The mint-time source of truth for `_standard_properties`/`_control_properties`'s
    `status` field (GH #134 D-6/S7): a Standard/Control's governance `status`
    mirrors its root Policy's `status`, never a hardcoded default.
    """
    return {
        node.id: _require_str_property(node, "status")
        for node in seed.nodes
        if node.label == "Policy"
    }


def _index_requirement_obligations(seed: InternalRegulationSeed) -> dict[str, tuple[str, ...]]:
    """Requirement local id -> the local ids of every Obligation it SATISFIED_BY-links to."""
    obligations_by_requirement: dict[str, list[str]] = {}
    for edge in seed.edges:
        if edge.type != "SATISFIED_BY":
            continue
        obligations_by_requirement.setdefault(edge.from_.id, []).append(edge.to.id)
    return {
        requirement_id: tuple(obligation_ids)
        for requirement_id, obligation_ids in obligations_by_requirement.items()
    }


def _validate_and_index(seed: InternalRegulationSeed) -> _SeedIndex:
    """Run every fail-closed check and return the index minting/persistence needs.

    Nothing in this function (or anything it calls) issues a `graph.query()`
    call -- confirmed by `test_dangling_requires_edge_fails_closed_no_partial_write`
    asserting zero recorded calls when this raises (AC-BI-011).
    """
    regulatory_instrument = find_regulatory_instrument(seed)
    _check_referential_integrity(seed)
    return _SeedIndex(
        regulatory_instrument=regulatory_instrument,
        obligation_bearer=_index_obligation_bearers(seed),
        requirement_source_ref=_index_requirement_source_refs(seed),
        requirement_obligation_ids=_index_requirement_obligations(seed),
        policy_governor=_index_policy_governors(seed),
        standard_supporter=_index_standard_supporters(seed),
        control_implementer=_index_control_implementers(seed),
    )


# --- canonical-id minting ---


def _require_str_property(node: SeedNode, key: str) -> str:
    value = node.properties.get(key)
    if not isinstance(value, str) or not value:
        raise InternalSeedError(f"{node.label} {node.id!r} is missing required property {key!r}")
    return value


def _mint_role_ids(seed: InternalRegulationSeed, regulatory_instrument_id: str) -> dict[str, str]:
    from ps_service.domain_mapper.identity import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        role_id,
    )

    return {
        node.id: role_id(_require_str_property(node, "name"), regulatory_instrument_id)
        for node in seed.nodes
        if node.label == "Role"
    }


def _mint_capability_ids(seed: InternalRegulationSeed) -> dict[str, str]:
    from ps_service.domain_mapper.identity import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        capability_id,
    )

    return {
        node.id: capability_id(_require_str_property(node, "name"))
        for node in seed.nodes
        if node.label == "Capability"
    }


def _mint_practice_area_ids(seed: InternalRegulationSeed) -> dict[str, str]:
    from ps_service.domain_mapper.identity import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        practice_area_id,
    )

    return {
        node.id: practice_area_id(_require_str_property(node, "name"))
        for node in seed.nodes
        if node.label == "PracticeArea"
    }


def _mint_risk_path_ids(seed: InternalRegulationSeed) -> dict[str, str]:
    from ps_service.domain_mapper.identity import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        risk_path_id,
    )

    return {
        node.id: risk_path_id(_require_str_property(node, "name"))
        for node in seed.nodes
        if node.label == "RiskPath"
    }


def _mint_policy_ids(seed: InternalRegulationSeed) -> dict[str, str]:
    from ps_service.domain_mapper.identity import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        policy_id,
    )

    return {
        node.id: policy_id(_require_str_property(node, "title"))
        for node in seed.nodes
        if node.label == "Policy"
    }


def _mint_standard_ids(
    seed: InternalRegulationSeed, index: _SeedIndex, policy_canonical_ids: dict[str, str]
) -> dict[str, str]:
    from ps_service.domain_mapper.identity import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        standard_id,
    )

    result: dict[str, str] = {}
    for node in seed.nodes:
        if node.label != "Standard":
            continue
        supporter_local_id = index.standard_supporter[node.id]
        supporter_canonical_id = policy_canonical_ids[supporter_local_id]
        title = _require_str_property(node, "title")
        result[node.id] = standard_id(supporter_canonical_id, title)
    return result


def _mint_control_ids(
    seed: InternalRegulationSeed, index: _SeedIndex, standard_canonical_ids: dict[str, str]
) -> dict[str, str]:
    from ps_service.domain_mapper.identity import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        control_id,
    )

    result: dict[str, str] = {}
    for node in seed.nodes:
        if node.label != "Control":
            continue
        implementer_local_id = index.control_implementer[node.id]
        implementer_canonical_id = standard_canonical_ids[implementer_local_id]
        title = _require_str_property(node, "title")
        result[node.id] = control_id(implementer_canonical_id, title)
    return result


def _mint_obligation_ids(
    seed: InternalRegulationSeed, index: _SeedIndex, role_canonical_ids: dict[str, str]
) -> dict[str, str]:
    from ps_service.domain_mapper.identity import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Domain Mapper at import
        obligation_id,
    )

    result: dict[str, str] = {}
    for node in seed.nodes:
        if node.label != "Obligation":
            continue
        bearer_local_id = index.obligation_bearer[node.id]
        bearer_canonical_id = role_canonical_ids[bearer_local_id]
        text = _require_str_property(node, "text")
        result[node.id] = obligation_id(bearer_canonical_id, text)
    return result


def _mint_requirement_ids(
    seed: InternalRegulationSeed, regulatory_instrument_id: str, index: _SeedIndex
) -> dict[str, str]:
    return {
        node.id: _requirement_id(regulatory_instrument_id, index.requirement_source_ref[node.id])
        for node in seed.nodes
        if node.label == "Requirement"
    }


def _resolve_requirement_role_id(
    requirement_local_id: str, index: _SeedIndex, role_canonical_ids: dict[str, str]
) -> str | None:
    """D6: the Requirement's `role_id` bookkeeping property, or `None` when ambiguous.

    Resolves `SATISFIED_BY -> Obligation <- HAS -> Role`. Returns the single
    bearing Role's canonical id when every Obligation this Requirement
    satisfies is borne by the *same* Role; returns `None` (never a string
    to be written as `null`) when the chain resolves to zero or more than
    one distinct Role.
    """
    obligation_ids = index.requirement_obligation_ids.get(requirement_local_id, ())
    bearer_local_ids = {
        index.obligation_bearer[obligation_id]
        for obligation_id in obligation_ids
        if obligation_id in index.obligation_bearer
    }
    if len(bearer_local_ids) != 1:
        return None
    (bearer_local_id,) = bearer_local_ids
    return role_canonical_ids.get(bearer_local_id)


# --- canonical property shaping (per `ps-domain-concepts.md`) ---


def _regulatory_instrument_properties(node: SeedNode) -> dict[str, object]:
    properties: dict[str, object] = {
        "title": _require_str_property(node, "title"),
        "source_type": _require_str_property(node, "source_type"),
        "effective_date": _require_str_property(node, "effective_date"),
        "version": _require_str_property(node, "version"),
        "status": _require_str_property(node, "status"),
    }
    jurisdiction = node.properties.get("jurisdiction")
    if jurisdiction is not None:
        properties["jurisdiction"] = jurisdiction
    return properties


def _role_properties(node: SeedNode) -> dict[str, object]:
    properties: dict[str, object] = {
        "name": _require_str_property(node, "name"),
        "confidence": node.properties.get("confidence", _DEFAULT_CONFIDENCE),
    }
    description = node.properties.get("description")
    if description is not None:
        properties["description"] = description
    return properties


def _requirement_properties(node: SeedNode, role_id_value: str | None) -> dict[str, object]:
    properties: dict[str, object] = {
        "text": _require_str_property(node, "text"),
        "type": _require_str_property(node, "type"),
        "confidence": node.properties.get("confidence", _DEFAULT_CONFIDENCE),
    }
    status = node.properties.get("status")
    if status is not None:
        properties["status"] = status
    if role_id_value is not None:  # D6/MAJOR-B: never write a literal null
        properties["role_id"] = role_id_value
    return properties


def _obligation_properties(node: SeedNode) -> dict[str, object]:
    return {
        "text": _require_str_property(node, "text"),
        "confidence": node.properties.get("confidence", _DEFAULT_CONFIDENCE),
    }


def _capability_properties(node: SeedNode) -> dict[str, object]:
    properties: dict[str, object] = {
        "name": _require_str_property(node, "name"),
        "confidence": node.properties.get("confidence", _DEFAULT_CONFIDENCE),
    }
    description = node.properties.get("description")
    if description is not None:
        properties["description"] = description
    capability_type = node.properties.get("type")
    if capability_type is not None:
        properties["type"] = capability_type
    return properties


def _policy_properties(node: SeedNode) -> dict[str, object]:
    """Required `title`/`status`, plus optional and structured properties.

    Optional: `description`/`owner_id`/`version`. Structured authoring-rubric
    fields: `scope_in`, `scope_out`, `normative_commitments`,
    `review_cadence`, `exception_pathway`, `measurable_outcomes`,
    `capability_grouping_rationale` -- see
    `ps-skills/ps-plugin/rubrics/policy-rubric.md`.

    Deliberately never sets a `confidence` key (Design Decision 2, PLAN.md
    §3) -- an authored Policy carries no LLM-derivation uncertainty.
    """
    properties: dict[str, object] = {
        "title": _require_str_property(node, "title"),
        "status": _require_str_property(node, "status"),
    }
    for optional_key in (
        "description",
        "owner_id",
        "version",
        "scope_in",
        "scope_out",
        "normative_commitments",
        "review_cadence",
        "exception_pathway",
        "measurable_outcomes",
        "capability_grouping_rationale",
    ):
        value = node.properties.get(optional_key)
        if value is not None:
            properties[optional_key] = value
    return properties


def _practice_area_properties(node: SeedNode) -> dict[str, object]:
    """Required `name`/`status`, optional `description`/`version`/`owner_id`.

    Deliberately never sets a `confidence` key (Design Decision 2, PLAN.md
    §3) -- an authored PracticeArea carries no LLM-derivation uncertainty,
    mirroring Policy/Standard/Control's omission. Mirrors `_policy_properties`'s
    shape.
    """
    properties: dict[str, object] = {
        "name": _require_str_property(node, "name"),
        "status": _require_str_property(node, "status"),
    }
    for optional_key in ("description", "version", "owner_id"):
        value = node.properties.get(optional_key)
        if value is not None:
            properties[optional_key] = value
    return properties


def _risk_path_properties(node: SeedNode) -> dict[str, object]:
    """Required `name`/`status`, optional `description`/`risk_type`/`version`.

    Deliberately never sets a `confidence` key (Design Decision 2, PLAN.md
    §3) -- an authored RiskPath carries no LLM-derivation uncertainty,
    mirroring PracticeArea/Policy/Standard/Control's omission. Mirrors
    `_practice_area_properties`'s shape.
    """
    properties: dict[str, object] = {
        "name": _require_str_property(node, "name"),
        "status": _require_str_property(node, "status"),
    }
    for optional_key in ("description", "risk_type", "version"):
        value = node.properties.get(optional_key)
        if value is not None:
            properties[optional_key] = value
    return properties


def _standard_properties(node: SeedNode, policy_status: str) -> dict[str, object]:
    """Required `title`/`implementation_status`/`status`, plus optional/structured properties.

    Optional: `description`/`version`. Structured authoring-rubric fields:
    `procedure`, `implementer_role`, `reviewer_role`, `applicability_boundary`,
    `verification_notes`, `change_rationale` -- see
    `ps-skills/ps-plugin/rubrics/standard-rubric.md`.

    `status` (governance status, independent of `implementation_status`) is
    never read off the seed node itself -- it mirrors the root Policy's own
    `status` at mint time (GH #134 D-6/S7), passed in by the caller, which
    has already resolved the Standard's supporting Policy via `_SeedIndex`.

    Deliberately never sets a `confidence` key (Design Decision 2, PLAN.md
    §3) -- an authored Standard carries no LLM-derivation uncertainty.
    Mirrors `_policy_properties`'s shape.
    """
    properties: dict[str, object] = {
        "title": _require_str_property(node, "title"),
        "implementation_status": _require_str_property(node, "implementation_status"),
        "status": policy_status,
    }
    for optional_key in (
        "description",
        "version",
        "procedure",
        "implementer_role",
        "reviewer_role",
        "applicability_boundary",
        "verification_notes",
        "change_rationale",
    ):
        value = node.properties.get(optional_key)
        if value is not None:
            properties[optional_key] = value
    return properties


def _control_properties(node: SeedNode, policy_status: str) -> dict[str, object]:
    """Required `type`/`title`/`implementation_status`/`status`, plus optional/structured props.

    Optional: the operational fields (`execution_frequency`, `last_test_date`,
    `next_review_date`, `evidence_ref`). Structured authoring-rubric fields:
    `pass_fail_criteria`, `execution_method`, `evidence_plan`, `executor_role`,
    `reviewer_role`, `risk_alignment_rationale` -- see
    `ps-skills/ps-plugin/rubrics/control-rubric.md`. `evidence_plan` is
    the authoring-time prose describing intended evidence; `evidence_ref`
    stays the operational pointer populated once real evidence exists.

    `status` (governance status, independent of `implementation_status`) is
    never read off the seed node itself -- it mirrors the root Policy's own
    `status` at mint time (GH #134 D-6/S7), resolved transitively through the
    Control's implemented Standard, passed in by the caller.

    Deliberately never sets a `confidence` key (Design Decision 2, PLAN.md
    §3) -- an authored Control carries no LLM-derivation uncertainty.
    Mirrors `_standard_properties`'s shape.
    """
    properties: dict[str, object] = {
        "type": _require_str_property(node, "type"),
        "title": _require_str_property(node, "title"),
        "implementation_status": _require_str_property(node, "implementation_status"),
        "status": policy_status,
    }
    for optional_key in (
        "description",
        "execution_frequency",
        "last_test_date",
        "next_review_date",
        "evidence_ref",
        "pass_fail_criteria",
        "execution_method",
        "evidence_plan",
        "executor_role",
        "reviewer_role",
        "risk_alignment_rationale",
    ):
        value = node.properties.get(optional_key)
        if value is not None:
            properties[optional_key] = value
    return properties


# --- Graph Write Gateway boundary ---


def _unstorable(what: str) -> InternalSeedError:
    return InternalSeedError(
        f"{what} has a property the graph cannot store "
        "(a reserved key, or a value that is not finite or is out of range)"
    )


def _upsert_node(label: str, node_id: str, properties: dict[str, object]) -> UpsertNode:
    """Build one node primitive, or raise `InternalSeedError` if the gateway cannot store it.

    The gateway reserves the property keys `id` and `embedding` and rejects non-finite floats;
    the legacy `SET n += $properties` accepted them (a property named `id` silently rewrote the
    node id), so they are rejected here, before anything is submitted (issue #208, G5).
    """
    try:
        return UpsertNode(label=label, id=node_id, properties=properties)
    except ValidationError as exc:
        raise _unstorable(f"{label} {node_id!r}") from exc


def _upsert_edge(
    edge_type: str,
    source: tuple[str, str],
    target: tuple[str, str],
    properties: dict[str, object],
) -> UpsertEdge:
    """Build one edge primitive, or raise `InternalSeedError` if the gateway cannot store it."""
    try:
        return UpsertEdge(
            type=edge_type,
            identity=edge_identity(edge_type, source[1], target[1]),
            source=NodeRef(label=source[0], id=source[1]),
            target=NodeRef(label=target[0], id=target[1]),
            properties=properties,
        )
    except ValidationError as exc:
        raise _unstorable(f"{edge_type} edge {source[1]!r} -> {target[1]!r}") from exc


def _without_dangling_edges(
    nodes: tuple[UpsertNode, ...], edges: tuple[UpsertEdge, ...]
) -> tuple[UpsertEdge, ...]:
    """Keep only the edges whose two endpoints are among `nodes` (AC-BI-004, silently).

    The gateway rejects a group holding an edge to a node that exists nowhere, where the legacy
    `MATCH ... MERGE` matched zero rows and wrote nothing. Dropped edges are not submitted, so
    nothing is logged or raised for them. A seed validated by `_validate_and_index` declares every
    endpoint, so the nodes of the document are exactly the nodes an edge may point at.
    """
    present = {(node.label, node.id) for node in nodes}
    return tuple(
        edge
        for edge in edges
        if (edge.source.label, edge.source.id) in present
        and (edge.target.label, edge.target.id) in present
    )


def build_native_primitives(seed: InternalRegulationSeed) -> tuple[Primitive, ...]:
    """The native graph's mutations: the raw submission verbatim (B5), nodes before edges.

    No remapping, no minting -- exactly the labels, ids and properties submitted, in document
    order. Labels and edge types are already constrained to the intake vocabulary by the seed
    model's `Literal` types.

    Raises:
        InternalSeedError: a node or edge carries a property the gateway cannot store.
    """
    nodes = tuple(_upsert_node(node.label, node.id, dict(node.properties)) for node in seed.nodes)
    edges = tuple(
        _upsert_edge(
            edge.type,
            (edge.from_.label, edge.from_.id),
            (edge.to.label, edge.to.id),
            dict(edge.properties),
        )
        for edge in seed.edges
    )
    return (*nodes, *_without_dangling_edges(nodes, edges))


def _submit_group(
    submitter: GroupSubmitter,
    *,
    graph_name: str,
    audit_event_id: str,
    plan: GroupPlan,
) -> None:
    """Submit `plan` as one group on `graph_name`; raise `InternalSeedError` on failure.

    The group requires the log to still be where the plan's read found it (plus the entries the
    legacy-edge backfill may log first), so a writer that got in between is refused
    with nothing written instead of being reconciled over.
    """
    stamp = stamp_legacy_edges_before_first_write(
        submitter, graph_name, audit_event_id, InternalSeedError
    )
    expected = plan.log_position + stamp.stamped  # a stamp logs one entry per edge it stamps
    group = MutationGroup(
        graph=graph_name,
        audit_event_id=audit_event_id,
        primitives=plan.primitives,
        preconditions=(ExpectedPosition(position=expected),),
        checkpoint_requested=True,  # the last group the seed writes to this graph (#215)
    )
    submit_or_raise(submitter, group, InternalSeedError)


@dataclass(frozen=True, slots=True)
class _CanonicalIds:
    """Every node's local id -> canonical id, one map per label."""

    regulatory_instrument_id: str
    role: dict[str, str]
    requirement: dict[str, str]
    obligation: dict[str, str]
    capability: dict[str, str]
    policy: dict[str, str]
    standard: dict[str, str]
    control: dict[str, str]
    practice_area: dict[str, str]
    risk_path: dict[str, str]

    def resolve(self, ref: SeedRef) -> str:
        """Return `ref`'s canonical id, dispatching on its own declared label.

        A dict-of-maps dispatch (rather than an if/elif chain) keeps this
        under the L1/L2 cyclomatic-complexity ceiling as new labels are
        added slice by slice -- `RegulatoryInstrument` is the one label
        with no per-node map (its id is fixed at parse time), so it stays
        a single early return.
        """
        if ref.label == "RegulatoryInstrument":
            return self.regulatory_instrument_id
        by_label: dict[str, dict[str, str]] = {
            "Role": self.role,
            "Requirement": self.requirement,
            "Obligation": self.obligation,
            "Capability": self.capability,
            "Policy": self.policy,
            "Standard": self.standard,
            "Control": self.control,
            "PracticeArea": self.practice_area,
            "RiskPath": self.risk_path,
        }
        return by_label[ref.label][ref.id]


@dataclass(frozen=True, slots=True)
class _BaselineContext:
    """What turning a seed node into a minted baseline node needs, resolved once per document."""

    index: _SeedIndex
    canonical: _CanonicalIds
    policy_status: dict[str, str]
    """Policy local id -> its `status`; a Standard/Control mirrors its root Policy's (GH #134)."""


type _NodeContent = tuple[str, dict[str, object]]
"""A baseline node's canonical id and its property map."""


def _role_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    return context.canonical.role[node.id], _role_properties(node)


def _capability_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    return context.canonical.capability[node.id], _capability_properties(node)


def _policy_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    return context.canonical.policy[node.id], _policy_properties(node)


def _standard_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    supporting_policy = context.index.standard_supporter[node.id]
    properties = _standard_properties(node, context.policy_status[supporting_policy])
    return context.canonical.standard[node.id], properties


def _control_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    implemented_standard = context.index.control_implementer[node.id]
    supporting_policy = context.index.standard_supporter[implemented_standard]
    properties = _control_properties(node, context.policy_status[supporting_policy])
    return context.canonical.control[node.id], properties


_REFERENCE_CONTENT: dict[str, Callable[[SeedNode, _BaselineContext], _NodeContent]] = {
    "Role": _role_content,
    "Capability": _capability_content,
    "Policy": _policy_content,
    "Standard": _standard_content,
    "Control": _control_content,
}
"""The labels other nodes and edges point at, which must exist before anything points at them."""


def _obligation_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    return context.canonical.obligation[node.id], _obligation_properties(node)


def _requirement_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    """D6: `role_id` is resolved through the bearing Role and omitted, never null, if ambiguous."""
    role_id_value = _resolve_requirement_role_id(node.id, context.index, context.canonical.role)
    return context.canonical.requirement[node.id], _requirement_properties(node, role_id_value)


def _practice_area_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    return context.canonical.practice_area[node.id], _practice_area_properties(node)


def _risk_path_content(node: SeedNode, context: _BaselineContext) -> _NodeContent:
    return context.canonical.risk_path[node.id], _risk_path_properties(node)


_DEPENDENT_CONTENT: dict[str, Callable[[SeedNode, _BaselineContext], _NodeContent]] = {
    "Obligation": _obligation_content,
    "Requirement": _requirement_content,
}
"""Nodes whose properties depend on the reference nodes (D6's `role_id` needs Role's id)."""

_CLASSIFICATION_CONTENT: dict[str, Callable[[SeedNode, _BaselineContext], _NodeContent]] = {
    "PracticeArea": _practice_area_content,
    "RiskPath": _risk_path_content,
}
"""Nodes that need no edge to exist (GH #93), so need no ordering among the others."""


def _content_nodes(
    seed: InternalRegulationSeed,
    context: _BaselineContext,
    content: dict[str, Callable[[SeedNode, _BaselineContext], _NodeContent]],
) -> tuple[UpsertNode, ...]:
    """Upsert every seed node whose label is in `content`, in document order."""
    return tuple(
        _upsert_node(node.label, node_id, properties)
        for node in seed.nodes
        if node.label in content
        for node_id, properties in (content[node.label](node, context),)
    )


def _baseline_nodes(
    seed: InternalRegulationSeed, index: _SeedIndex, canonical: _CanonicalIds
) -> tuple[UpsertNode, ...]:
    """The baseline graph's nodes, with canonical (minted) ids, in a fixed order.

    The RegulatoryInstrument first, then Role, Capability, Policy, Standard and Control in
    document order (the labels other nodes and edges point at), then Obligation and Requirement,
    then PracticeArea and RiskPath.

    Raises:
        InternalSeedError: a node carries a property the gateway cannot store.
    """
    context = _BaselineContext(
        index=index, canonical=canonical, policy_status=_policy_status_by_local_id(seed)
    )
    instrument = _upsert_node(
        "RegulatoryInstrument",
        canonical.regulatory_instrument_id,
        _regulatory_instrument_properties(index.regulatory_instrument),
    )
    return (
        instrument,
        *_content_nodes(seed, context, _REFERENCE_CONTENT),
        *_content_nodes(seed, context, _DEPENDENT_CONTENT),
        *_content_nodes(seed, context, _CLASSIFICATION_CONTENT),
    )


def _baseline_edges(
    seed: InternalRegulationSeed, canonical: _CanonicalIds
) -> tuple[UpsertEdge, ...]:
    """The baseline edges between canonical ids; only DEFINES and EXPRESSES keep `source_ref`."""
    return tuple(
        _upsert_edge(
            edge.type,
            (edge.from_.label, canonical.resolve(edge.from_)),
            (edge.to.label, canonical.resolve(edge.to)),
            {"source_ref": edge.properties["source_ref"]}
            if edge.type in ("DEFINES", "EXPRESSES")
            else {},
        )
        for edge in seed.edges
    )


def build_baseline_primitives(
    seed: InternalRegulationSeed, index: _SeedIndex, canonical: _CanonicalIds
) -> tuple[Primitive, ...]:
    """The baseline graph's mutations: the minted spine, every node before every edge.

    Raises:
        InternalSeedError: a node or edge carries a property the gateway cannot store.
    """
    nodes = _baseline_nodes(seed, index, canonical)
    return (*nodes, *_without_dangling_edges(nodes, _baseline_edges(seed, canonical)))


def _mint_canonical_ids(seed: InternalRegulationSeed, index: _SeedIndex) -> _CanonicalIds:
    """Mint every node's canonical id, label by label, in dependency order."""
    regulatory_instrument_id = index.regulatory_instrument.id
    role = _mint_role_ids(seed, regulatory_instrument_id)
    policy = _mint_policy_ids(seed)
    standard = _mint_standard_ids(seed, index, policy)
    return _CanonicalIds(
        regulatory_instrument_id=regulatory_instrument_id,
        role=role,
        requirement=_mint_requirement_ids(seed, regulatory_instrument_id, index),
        obligation=_mint_obligation_ids(seed, index, role),
        capability=_mint_capability_ids(seed),
        policy=policy,
        standard=standard,
        control=_mint_control_ids(seed, index, standard),
        practice_area=_mint_practice_area_ids(seed),
        risk_path=_mint_risk_path_ids(seed),
    )


# --- entry point ---


def ingest_internal_regulatory_instrument(
    seed: InternalRegulationSeed,
    *,
    submitter: GroupSubmitter,
    native_graph_name: str,
    baseline_graph_name: str,
    audit_event_id: str,
    emitter: LogEmitter | None = None,
) -> InternalIngestResult:
    """Validate, mint, and persist one internal-regulation seed (S2's one pipeline stage).

    Args:
        seed: The already-parsed submission (`adapter.read_seed`'s output).
        submitter: The Graph Write Gateway; the raw submission (B5) is submitted as one native
            mutation group on it, nodes then edges (issue #208).
        native_graph_name: The name of the `{short}_native` graph, the native group's target.
        baseline_graph_name: The name of the `{short}_baseline` graph, the baseline group's target.
        audit_event_id: The id of the audit event of the command that caused this run; the
            group is logged under it.
        emitter: Reserved for future per-node audit logging; unused today.

    Returns:
        An `InternalIngestResult` naming the regulatory instrument id and
        the count of each minted node label.

    Raises:
        InternalSeedError: The document fails referential integrity or
            cardinality validation (AC-BI-011) -- raised with zero
            writes made to either graph -- or the instrument carries a
            node or edge property the gateway cannot store (a reserved key,
            a non-finite number), or a graph write fails
            (message `FalkorDB write failed: ...`).
    """
    _ = emitter
    index = _validate_and_index(seed)
    canonical = _mint_canonical_ids(seed, index)

    native_primitives = build_native_primitives(seed)
    baseline_primitives = build_baseline_primitives(seed, index, canonical)
    targets = (
        (native_graph_name, native_primitives),
        (baseline_graph_name, baseline_primitives),
    )
    # Every read precedes the first write, so a lagging graph or a failed read leaves the log
    # of both graphs untouched.
    plans = [prepare_group(submitter, graph, primitives) for graph, primitives in targets]
    for (graph, _), plan in zip(targets, plans, strict=True):
        _submit_group(submitter, graph_name=graph, audit_event_id=audit_event_id, plan=plan)

    return InternalIngestResult(
        regulatory_instrument_id=canonical.regulatory_instrument_id,
        role_count=len(canonical.role),
        requirement_count=len(canonical.requirement),
        obligation_count=len(canonical.obligation),
        capability_count=len(canonical.capability),
        policy_count=len(canonical.policy),
        standard_count=len(canonical.standard),
        control_count=len(canonical.control),
        practice_area_count=len(canonical.practice_area),
        risk_path_count=len(canonical.risk_path),
    )
