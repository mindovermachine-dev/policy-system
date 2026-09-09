"""FalkorDB persistence for `ps_service.domain_mapper`'s baseline graph.

`persist_role_and_requirement_graph` (PLAN_REVIEWED.md §11 Increment 9,
§5.4) and `persist_obligation_and_capability_graph` (§11 Increment 15,
§7.6).

**B3 fix (a) — validate-then-write, mirroring `ps_service.ingestion.
graph_writer`'s own B1 fix exactly** (read that module directly for the
precedent this one follows one component over):

- Every Role node, every `DEFINES` edge, every Requirement node (each
  carrying its `role_id` bookkeeping property, §7.2), every `EXPRESSES`
  edge is built completely in memory by the caller (`extraction.py`'s
  `_canonicalize_roles`/`_build_requirement_graph`, Increment 8) BEFORE
  this function issues any `graph.query()` call.
- `_validate_role_references` checks every about-to-be-persisted
  Requirement node's `role_id` property resolves to the `id` of some
  about-to-be-persisted Role node in this SAME call, raising
  `DomainMapperPersistenceError` (naming the offending Requirement id and
  its dangling `role_id`) on the first violation found — zero
  `graph.query()` calls have been made by the time this raises, the same
  guarantee `ps_service.ingestion.graph_writer._validate_element_types`
  gives Ingestion.
- Only once validation passes does this function loop and write:
  RegulatoryInstrument MERGE, Role node MERGEs, `DEFINES` edge MERGEs, Requirement
  node MERGEs (with `role_id` in `properties`), `EXPRESSES` edge MERGEs —
  all parameterized (`source_ref`/`role_id`/etc. in `params`, never
  interpolated into the query string).

**Design note (S2's resolution, PLAN_REVIEWED.md §0.5 Divergence #1 /
§11 Increment 9) — no query-safety allow-list is needed anywhere in this
module.** `Role`, `Requirement`, `RegulatoryInstrument`, `DEFINES`, `EXPRESSES` are
all fixed Python literals in this module's own source, interpolated as
labels/relationship-type names the same way `ps_service.ingestion.
graph_writer`'s `_REGULATORY_INSTRUMENT_LABEL` is (no allow-list needed there either)
— unlike that module's `_upsert_node`/`_upsert_edge`, which interpolate an
adapter-supplied `element_type` string and therefore DO need
`_validate_element_types`'s allow-list. Nothing in this module's write path
ever accepts an externally- or LLM-sourced string into a label/relationship-
type position (`role_name`/requirement `text`/etc. all flow through
`params`, never through an f-string into the query), so there is nothing
pytest-executable to assert here today. If a future change introduces one
(e.g. a label parameterized by `Role.name` for some reporting feature),
that change should add its own allow-list and its own test at that time,
the same way issue #14 did when `element_type` first became a label. This
is stated here as a design note, not backed by a test.

**`persist_obligation_and_capability_graph` (Increment 15, §7.6) — no
validate-then-write pass, and why.** Unlike `persist_role_and_requirement_graph`'s
Requirement `role_id` property (an independently-computed bookkeeping value
set by a DIFFERENT stage, `extraction.py`'s canonicalization, than the one
that builds `role_nodes` — the exact decoupling that created B3's dangling-
reference risk), every `obligation_node_id`/`capability_node_id` referenced
by `has_edges`/`satisfied_by_edges`/`requires_edges` here is guaranteed
self-consistent BY CONSTRUCTION within `derivation.py`'s own whole-run
algorithm: `_process_requirement` appends to `state.has_edges` in the same
branch, and immediately alongside, it appends to `state.obligation_nodes`
(never independently); `_derive_capabilities` appends to `requires_edges`
in the same loop iteration it adds a new id to `capability_nodes`/
`registry`. There is no code path that produces one of these edge
collections without also producing the matching node — nothing analogous
to Increment 9's "a Requirement's `role_id` could point past the
`role_nodes` this same call happens to be given" gap exists here for
Obligation/Capability. The `HAS`/`SATISFIED_BY` edges' OTHER endpoint
(`role_node_id`/`requirement_id`) points outside this call's own node
collections entirely (Role/Requirement were persisted by a PRIOR
`persist_role_and_requirement_graph` call) — validating that reference
in-memory here would be a category error (this function is never given any
Role/Requirement collection to validate against); the real defense for
THAT reference is the read-side dangling-`role_id` check
(`_read_requirements_by_role`, PLAN_REVIEWED.md §7.2's B3 fix (b)),
performed by the orchestrating `derive_obligations_and_capabilities`
(Increment 16) before any LLM call is even made — i.e. before any
`RoleRequirements` (and therefore any Obligation/edge derived from them)
can exist at all. Given both of those, this function is a plain,
unconditional writer, matching the style of `_upsert_node`/
`_upsert_regulatory_instrument_edge` above but with no properties on any of its three
edge types (Edge Catalog, §0.2) — deliberately NOT copying
`_upsert_regulatory_instrument_edge`'s `SET e.source_ref = $source_ref` pattern, since
`HAS`/`SATISFIED_BY`/`REQUIRES` carry no properties at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.domain_mapper.errors import DomainMapperPersistenceError
from ps_service.domain_mapper.models import (
    CapabilityNode,
    CapabilityRequiresEdge,
    ObligationHasEdge,
    ObligationNode,
    RequirementExpressesEdge,
    RequirementNode,
    RequirementSatisfiedByEdge,
    RoleDefinesEdge,
    RoleNode,
)

if TYPE_CHECKING:
    from ps_service.domain_mapper.falkordb_client import GraphHandle, GraphQueryResult

_REGULATORY_INSTRUMENT_LABEL = "RegulatoryInstrument"
_ROLE_LABEL = "Role"
_REQUIREMENT_LABEL = "Requirement"
_OBLIGATION_LABEL = "Obligation"
_CAPABILITY_LABEL = "Capability"


def _execute_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """The one call site every `graph.query()` write in this module goes through.

    So FalkorDB connectivity failures get recorded in
    `ps_service.dependency_health` — mirrors
    `ps_service.ingestion.graph_writer._execute_query` exactly.

    Wraps `redis.exceptions.RedisError` into `DomainMapperPersistenceError`,
    distinct from this module's own data-validation
    `DomainMapperPersistenceError`s (`_validate_role_references`), which are
    raised directly by this module's own logic and never reach here.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise DomainMapperPersistenceError(f"FalkorDB write failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return result


def _validate_role_references(
    role_nodes: tuple[RoleNode, ...], requirement_nodes: tuple[RequirementNode, ...]
) -> None:
    """B3 fix (a): a whole-collection validation pass, called once before any write.

    Runs before `persist_role_and_requirement_graph` issues a single
    `graph.query` call for ANY element — not interleaved
    validate-then-write per element.

    Checks every Requirement node's `role_id` property against the set of
    Role node ids ALSO about to be persisted in this same call. Raises
    `DomainMapperPersistenceError` on the first violation found, with zero
    `graph.query` calls having been made by the time this raises.
    """
    role_ids = {node.id for node in role_nodes}
    for requirement in requirement_nodes:
        dangling_role_id = requirement.properties.get("role_id")
        if dangling_role_id not in role_ids:
            raise DomainMapperPersistenceError(
                f"Requirement {requirement.id!r} references role_id "
                f"{dangling_role_id!r}, which is not among the Role nodes "
                "being persisted in this call"
            )


def persist_role_and_requirement_graph(
    graph: GraphHandle,
    regulatory_instrument_id: str,
    regulatory_instrument_properties: dict[str, object],
    role_nodes: tuple[RoleNode, ...],
    role_edges: tuple[RoleDefinesEdge, ...],
    requirement_nodes: tuple[RequirementNode, ...],
    requirement_edges: tuple[RequirementExpressesEdge, ...],
) -> None:
    """Persist one extraction run's complete Role/Requirement graph into `graph`.

    `graph` is the caller's already-selected `{short}_baseline`
    `GraphHandle`. `regulatory_instrument_properties` MERGEs the
    RegulatoryInstrument node itself (PLAN_REVIEWED.md §5.2 step 1) — the
    `DEFINES`/`EXPRESSES` edges below originate from it.
    `role_nodes`/`role_edges`/`requirement_nodes`/`requirement_edges` are
    `extraction.py`'s `_canonicalize_roles`/`_build_requirement_graph`
    output (Increment 8) — this function does no canonicalization or
    collision handling of its own, only validation and writing.

    Raises `DomainMapperPersistenceError` (via `_validate_role_references`)
    before any write if a Requirement's `role_id` doesn't resolve within
    this call's own `role_nodes` — see the module docstring's B3 fix (a).
    """
    _validate_role_references(role_nodes, requirement_nodes)

    _execute_query(
        graph,
        f"MERGE (n:{_REGULATORY_INSTRUMENT_LABEL} {{id: $id}}) SET n += $properties",
        params={"id": regulatory_instrument_id, "properties": regulatory_instrument_properties},
    )
    for role in role_nodes:
        _upsert_node(graph, _ROLE_LABEL, role.id, role.properties)
    for role_edge in role_edges:
        _upsert_regulatory_instrument_edge(
            graph, "DEFINES", _ROLE_LABEL, regulatory_instrument_id, role_edge
        )
    for requirement in requirement_nodes:
        _upsert_node(graph, _REQUIREMENT_LABEL, requirement.id, requirement.properties)
    for requirement_edge in requirement_edges:
        _upsert_regulatory_instrument_edge(
            graph, "EXPRESSES", _REQUIREMENT_LABEL, regulatory_instrument_id, requirement_edge
        )


def _upsert_node(
    graph: GraphHandle, label: str, node_id: str, properties: dict[str, str | float]
) -> None:
    # label is always one of this module's own fixed literals (_ROLE_LABEL/
    # _REQUIREMENT_LABEL) — see the module docstring's "no allow-list
    # needed" design note.
    _execute_query(
        graph,
        f"MERGE (n:{label} {{id: $id}}) SET n += $properties",
        params={"id": node_id, "properties": properties},
    )


def _upsert_regulatory_instrument_edge(
    graph: GraphHandle,
    relationship_type: str,
    target_label: str,
    regulatory_instrument_id: str,
    edge: RoleDefinesEdge | RequirementExpressesEdge,
) -> None:
    """`RegulatoryInstrument -[:relationship_type {source_ref}]-> target_label`.

    Shared shape for both `DEFINES` (Role) and `EXPRESSES` (Requirement)
    edges — both originate from the RegulatoryInstrument node and carry the
    same single `source_ref` property (Edge Catalog, PLAN_REVIEWED.md
    §0.2).

    `relationship_type`/`target_label` are always this module's own fixed
    literals (`"DEFINES"`/`"EXPRESSES"`, `_ROLE_LABEL`/`_REQUIREMENT_LABEL`)
    — never adapter- or LLM-sourced — so no allow-list check applies here
    either. The target node's id and `source_ref` flow through `params`
    only, never interpolated into the query string.
    """
    target_node_id = (
        edge.role_node_id if isinstance(edge, RoleDefinesEdge) else edge.requirement_node_id
    )
    _execute_query(
        graph,
        f"MATCH (r:{_REGULATORY_INSTRUMENT_LABEL} {{id: $regulatory_instrument_id}}), "
        f"(n:{target_label} {{id: $target_id}}) "
        f"MERGE (r)-[e:{relationship_type}]->(n) SET e.source_ref = $source_ref",
        params={
            "regulatory_instrument_id": regulatory_instrument_id,
            "target_id": target_node_id,
            "source_ref": edge.source_ref,
        },
    )


def persist_obligation_and_capability_graph(
    graph: GraphHandle,
    obligation_nodes: tuple[ObligationNode, ...],
    has_edges: tuple[ObligationHasEdge, ...],
    satisfied_by_edges: tuple[RequirementSatisfiedByEdge, ...],
    capability_nodes: tuple[CapabilityNode, ...],
    requires_edges: tuple[CapabilityRequiresEdge, ...],
) -> None:
    """Persist one derivation run's complete Obligation/Capability graph into `graph`.

    `graph` is the caller's already-selected `{short}_baseline`
    `GraphHandle` — the SAME graph a prior
    `persist_role_and_requirement_graph` call already wrote
    Role/Requirement nodes into.

    `obligation_nodes`/`has_edges`/`satisfied_by_edges` are `derivation.py`'s
    `_derive_obligations` output (Increment 12); `capability_nodes`/
    `requires_edges` are `_derive_capabilities`'s output (Increment 14),
    called with that same `obligation_nodes` tuple (PLAN_REVIEWED.md §11
    Increment 16 wires the two together). This function does no
    mint/match/collision logic of its own, only writing.

    Nodes are written before any edge that references them (`HAS`/
    `SATISFIED_BY`/`REQUIRES` all `MATCH` their endpoints rather than
    `MERGE` them — a `MATCH` against a not-yet-written node silently
    matches zero rows and writes no edge, so write order here is
    load-bearing, not stylistic).

    Per the Edge Catalog (PLAN_REVIEWED.md §0.2), `HAS`/`SATISFIED_BY`/
    `REQUIRES` carry NO properties — deliberately distinct from `DEFINES`/
    `EXPRESSES`, which always carry `source_ref`. No `source_ref`, or any
    other property, is ever written onto these three edge types by this
    function; see `_upsert_bare_edge` below.

    No validate-then-write pass, unlike `persist_role_and_requirement_graph`
    — see this module's own docstring for the reasoning (in short:
    `has_edges`/`satisfied_by_edges`/`requires_edges`' Obligation/Capability
    endpoints are self-consistent by construction within `derivation.py`'s
    own whole-run algorithm, and their Role/Requirement endpoints are
    validated on the READ side by Increment 16's `_read_requirements_by_role`
    before any of this data can exist at all).
    """
    for obligation in obligation_nodes:
        _upsert_node(graph, _OBLIGATION_LABEL, obligation.id, obligation.properties)
    for capability in capability_nodes:
        _upsert_node(graph, _CAPABILITY_LABEL, capability.id, capability.properties)
    for has_edge in has_edges:
        _upsert_bare_edge(
            graph,
            "HAS",
            _ROLE_LABEL,
            has_edge.role_node_id,
            _OBLIGATION_LABEL,
            has_edge.obligation_node_id,
        )
    for satisfied_by_edge in satisfied_by_edges:
        _upsert_bare_edge(
            graph,
            "SATISFIED_BY",
            _REQUIREMENT_LABEL,
            satisfied_by_edge.requirement_id,
            _OBLIGATION_LABEL,
            satisfied_by_edge.obligation_node_id,
        )
    for requires_edge in requires_edges:
        _upsert_bare_edge(
            graph,
            "REQUIRES",
            _OBLIGATION_LABEL,
            requires_edge.obligation_node_id,
            _CAPABILITY_LABEL,
            requires_edge.capability_node_id,
        )


def _upsert_bare_edge(
    graph: GraphHandle,
    relationship_type: str,
    source_label: str,
    source_id: str,
    target_label: str,
    target_id: str,
) -> None:
    """`source_label -[:relationship_type]-> target_label`, NO properties.

    The shared shape for `HAS`/`SATISFIED_BY`/`REQUIRES` (Edge Catalog,
    PLAN_REVIEWED.md §0.2), deliberately distinct from
    `_upsert_regulatory_instrument_edge`'s `DEFINES`/`EXPRESSES` shape,
    which always sets `source_ref`. There is no `SET` clause here at all —
    be deliberate about NOT copy-pasting `_upsert_regulatory_instrument_edge`'s
    pattern onto this function (PLAN_REVIEWED.md §11 Increment 15's explicit
    warning).

    `relationship_type`/`source_label`/`target_label` are always this
    module's own fixed literals (`"HAS"`/`"SATISFIED_BY"`/`"REQUIRES"`,
    `_ROLE_LABEL`/`_REQUIREMENT_LABEL`/`_OBLIGATION_LABEL`/
    `_CAPABILITY_LABEL`) — never adapter- or LLM-sourced — so no allow-list
    check applies here either. `source_id`/`target_id` flow through
    `params` only, never interpolated into the query string.
    """
    _execute_query(
        graph,
        f"MATCH (s:{source_label} {{id: $source_id}}), "
        f"(t:{target_label} {{id: $target_id}}) "
        f"MERGE (s)-[:{relationship_type}]->(t)",
        params={"source_id": source_id, "target_id": target_id},
    )
