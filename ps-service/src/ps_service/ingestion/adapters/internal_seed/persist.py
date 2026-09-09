"""Referential validation, canonical-id minting, and dual-graph persistence (S2).

`ingest_internal_regulatory_instrument` is the internal-seed pipeline's single
stage (`PLAN.md` S2): given an already-parsed `InternalRegulationSeed`
(`adapter.read_seed`'s output), it

1. validates the whole document's referential integrity and cardinality
   rules -- every edge endpoint must be declared, every `Obligation` has
   exactly one bearing `Role`, every `Requirement` has exactly one
   `EXPRESSES` source -- entirely before any `graph.query()` call (AC-BI-011,
   mirroring `ingestion/graph_writer.py`'s own B1-fix validate-then-write
   shape: this function raises with zero writes recorded, or writes
   everything);
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

D6: `Requirement.properties["role_id"]` is synthesized here by resolving the
`SATISFIED_BY -> Obligation <- HAS -> Role` chain; when it resolves to more
than one distinct Role (or none), the property key is omitted entirely --
never set to `None`/`null`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError

if TYPE_CHECKING:
    from ps_service.ingestion.adapters.internal_seed.models import (
        EdgeType,
        InternalRegulationSeed,
        NodeLabel,
        SeedEdge,
        SeedNode,
        SeedRef,
    )
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
}


class GraphQueryResult(Protocol):
    """The one property this module reads off a Cypher result (none, today -- write-only)."""

    @property
    def result_set(self) -> list[object]:
        """The rows returned by the query."""
        ...


class GraphHandle(Protocol):
    """Structural stand-in for a FalkorDB graph handle."""

    def query(self, q: str, params: dict[str, object] | None = None) -> GraphQueryResult:
        """Run Cypher `q` (optionally parameterized via `params`) and return the result."""
        ...


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
    """Required `title`/`status`, optional `description`/`owner_id`/`version`.

    Deliberately never sets a `confidence` key (Design Decision 2, PLAN.md
    §3) -- an authored Policy carries no LLM-derivation uncertainty.
    """
    properties: dict[str, object] = {
        "title": _require_str_property(node, "title"),
        "status": _require_str_property(node, "status"),
    }
    for optional_key in ("description", "owner_id", "version"):
        value = node.properties.get(optional_key)
        if value is not None:
            properties[optional_key] = value
    return properties


def _standard_properties(node: SeedNode) -> dict[str, object]:
    """Required `title`/`implementation_status`, optional `description`/`version`.

    Deliberately never sets a `confidence` key (Design Decision 2, PLAN.md
    §3) -- an authored Standard carries no LLM-derivation uncertainty.
    Mirrors `_policy_properties`'s shape.
    """
    properties: dict[str, object] = {
        "title": _require_str_property(node, "title"),
        "implementation_status": _require_str_property(node, "implementation_status"),
    }
    for optional_key in ("description", "version"):
        value = node.properties.get(optional_key)
        if value is not None:
            properties[optional_key] = value
    return properties


def _control_properties(node: SeedNode) -> dict[str, object]:
    """Required `type`/`title`/`implementation_status`, optional operational fields.

    Deliberately never sets a `confidence` key (Design Decision 2, PLAN.md
    §3) -- an authored Control carries no LLM-derivation uncertainty.
    Mirrors `_standard_properties`'s shape.
    """
    properties: dict[str, object] = {
        "type": _require_str_property(node, "type"),
        "title": _require_str_property(node, "title"),
        "implementation_status": _require_str_property(node, "implementation_status"),
    }
    for optional_key in (
        "description",
        "execution_frequency",
        "last_test_date",
        "next_review_date",
        "evidence_ref",
    ):
        value = node.properties.get(optional_key)
        if value is not None:
            properties[optional_key] = value
    return properties


# --- FalkorDB write boundary ---


def _execute_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """The one call site every write in this module goes through.

    Mirrors `ingestion/graph_writer.py::_execute_query` exactly: records
    FalkorDB connectivity failures in `dependency_health` for `/ready`'s
    live signal, self-healing on the next successful call.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise InternalSeedError(f"FalkorDB write failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return result


def _persist_native(graph: GraphHandle, seed: InternalRegulationSeed) -> None:
    """Write the customer's raw, pre-minting, local-id submission verbatim (B5).

    No remapping, no minting -- exactly the labels/ids/properties submitted.
    Labels/edge types interpolated below are already constrained to the
    five-member allow-list by `SeedNode.label`/`SeedEdge.type`'s own
    `Literal` type (enforced at `adapter.read_seed`'s Pydantic-parse
    boundary) -- this function only ever issues the write, matching
    `ingestion/graph_writer.py`'s own "validated upstream, write here" split.
    """
    for node in seed.nodes:
        _execute_query(
            graph,
            f"MERGE (n:{node.label} {{id: $id}}) SET n += $properties",
            params={"id": node.id, "properties": dict(node.properties)},
        )
    for edge in seed.edges:
        _execute_query(
            graph,
            f"MATCH (a:{edge.from_.label} {{id: $from_id}}), "
            f"(b:{edge.to.label} {{id: $to_id}}) "
            f"MERGE (a)-[r:{edge.type}]->(b) SET r += $properties",
            params={
                "from_id": edge.from_.id,
                "to_id": edge.to.id,
                "properties": dict(edge.properties),
            },
        )


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
        }
        return by_label[ref.label][ref.id]


def _persist_baseline_reference_nodes(
    graph: GraphHandle, seed: InternalRegulationSeed, index: _SeedIndex, canonical: _CanonicalIds
) -> None:
    """Write RegulatoryInstrument/Role/Capability/Policy/Standard/Control.

    Split out of `_persist_baseline_nodes` to keep cyclomatic complexity
    <=8 (L1/L2 coding standards) -- the same `_index_*`-style splitting
    already used elsewhere in this module. These are the labels other
    nodes/edges reference (`GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY`
    point at Policy/Standard/Control), so they must exist before anything
    that could point at them -- the same ordering rule
    `_persist_baseline_dependent_nodes` documents for Obligation/Requirement
    below. Control is written last in this group (after Standard) since an
    `IMPLEMENTED_BY` edge points Standard -> Control.
    """
    _execute_query(
        graph,
        "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties",
        params={
            "id": canonical.regulatory_instrument_id,
            "properties": _regulatory_instrument_properties(index.regulatory_instrument),
        },
    )
    for node in seed.nodes:
        if node.label == "Role":
            _execute_query(
                graph,
                "MERGE (n:Role {id: $id}) SET n += $properties",
                params={"id": canonical.role[node.id], "properties": _role_properties(node)},
            )
        elif node.label == "Capability":
            _execute_query(
                graph,
                "MERGE (n:Capability {id: $id}) SET n += $properties",
                params={
                    "id": canonical.capability[node.id],
                    "properties": _capability_properties(node),
                },
            )
        elif node.label == "Policy":
            _execute_query(
                graph,
                "MERGE (n:Policy {id: $id}) SET n += $properties",
                params={
                    "id": canonical.policy[node.id],
                    "properties": _policy_properties(node),
                },
            )
        elif node.label == "Standard":
            _execute_query(
                graph,
                "MERGE (n:Standard {id: $id}) SET n += $properties",
                params={
                    "id": canonical.standard[node.id],
                    "properties": _standard_properties(node),
                },
            )
        elif node.label == "Control":
            _execute_query(
                graph,
                "MERGE (n:Control {id: $id}) SET n += $properties",
                params={
                    "id": canonical.control[node.id],
                    "properties": _control_properties(node),
                },
            )


def _persist_baseline_dependent_nodes(
    graph: GraphHandle, seed: InternalRegulationSeed, index: _SeedIndex, canonical: _CanonicalIds
) -> None:
    """Write Obligation/Requirement nodes.

    Their properties depend on the reference nodes
    `_persist_baseline_reference_nodes` already wrote (D6's `role_id`
    resolution needs Role's canonical id).
    """
    for node in seed.nodes:
        if node.label == "Obligation":
            _execute_query(
                graph,
                "MERGE (n:Obligation {id: $id}) SET n += $properties",
                params={
                    "id": canonical.obligation[node.id],
                    "properties": _obligation_properties(node),
                },
            )
        elif node.label == "Requirement":
            role_id_value = _resolve_requirement_role_id(node.id, index, canonical.role)
            _execute_query(
                graph,
                "MERGE (n:Requirement {id: $id}) SET n += $properties",
                params={
                    "id": canonical.requirement[node.id],
                    "properties": _requirement_properties(node, role_id_value),
                },
            )


def _persist_baseline_nodes(
    graph: GraphHandle, seed: InternalRegulationSeed, index: _SeedIndex, canonical: _CanonicalIds
) -> None:
    _persist_baseline_reference_nodes(graph, seed, index, canonical)
    _persist_baseline_dependent_nodes(graph, seed, index, canonical)


def _persist_baseline_edges(
    graph: GraphHandle, seed: InternalRegulationSeed, canonical: _CanonicalIds
) -> None:
    for edge in seed.edges:
        edge_properties = (
            {"source_ref": edge.properties["source_ref"]}
            if edge.type in ("DEFINES", "EXPRESSES")
            else {}
        )
        _execute_query(
            graph,
            f"MATCH (a:{edge.from_.label} {{id: $from_id}}), "
            f"(b:{edge.to.label} {{id: $to_id}}) "
            f"MERGE (a)-[r:{edge.type}]->(b) SET r += $properties",
            params={
                "from_id": canonical.resolve(edge.from_),
                "to_id": canonical.resolve(edge.to),
                "properties": edge_properties,
            },
        )


def _persist_baseline(
    graph: GraphHandle, seed: InternalRegulationSeed, index: _SeedIndex, canonical: _CanonicalIds
) -> None:
    """Write the minted, remapped spine: canonical ids, add/merge-only (AC-BI-012)."""
    _persist_baseline_nodes(graph, seed, index, canonical)
    _persist_baseline_edges(graph, seed, canonical)


# --- entry point ---


def ingest_internal_regulatory_instrument(
    seed: InternalRegulationSeed,
    *,
    baseline_graph: GraphHandle,
    native_graph: GraphHandle,
    emitter: LogEmitter | None = None,
) -> InternalIngestResult:
    """Validate, mint, and persist one internal-regulation seed (S2's one pipeline stage).

    Args:
        seed: The already-parsed submission (`adapter.read_seed`'s output).
        baseline_graph: The `{short}_baseline` graph handle to write the
            minted spine to.
        native_graph: The `{short}_native` graph handle to write the raw
            submission to, verbatim (B5).
        emitter: Reserved for future per-node audit logging; unused today.

    Returns:
        An `InternalIngestResult` naming the regulatory instrument id and
        the count of each minted node label.

    Raises:
        InternalSeedError: The document fails referential integrity or
            cardinality validation (AC-BI-011) -- raised with zero
            `graph.query()` calls made on either graph -- or a FalkorDB
            write itself fails.
    """
    _ = emitter
    index = _validate_and_index(seed)
    regulatory_instrument_id = index.regulatory_instrument.id

    role_canonical_ids = _mint_role_ids(seed, regulatory_instrument_id)
    capability_canonical_ids = _mint_capability_ids(seed)
    policy_canonical_ids = _mint_policy_ids(seed)
    standard_canonical_ids = _mint_standard_ids(seed, index, policy_canonical_ids)
    control_canonical_ids = _mint_control_ids(seed, index, standard_canonical_ids)
    obligation_canonical_ids = _mint_obligation_ids(seed, index, role_canonical_ids)
    requirement_canonical_ids = _mint_requirement_ids(seed, regulatory_instrument_id, index)
    canonical = _CanonicalIds(
        regulatory_instrument_id=regulatory_instrument_id,
        role=role_canonical_ids,
        requirement=requirement_canonical_ids,
        obligation=obligation_canonical_ids,
        capability=capability_canonical_ids,
        policy=policy_canonical_ids,
        standard=standard_canonical_ids,
        control=control_canonical_ids,
    )

    _persist_native(native_graph, seed)
    _persist_baseline(baseline_graph, seed, index, canonical)

    return InternalIngestResult(
        regulatory_instrument_id=regulatory_instrument_id,
        role_count=len(role_canonical_ids),
        requirement_count=len(requirement_canonical_ids),
        obligation_count=len(obligation_canonical_ids),
        capability_count=len(capability_canonical_ids),
        policy_count=len(policy_canonical_ids),
        standard_count=len(standard_canonical_ids),
        control_count=len(control_canonical_ids),
    )
