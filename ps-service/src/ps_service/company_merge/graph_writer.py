"""FalkorDB persistence for `ps_service.company_merge`'s single-tenant graph.

`persist_role_and_requirement_passthrough` (PLAN_REVIEWED.md §10 Increment
10, §6) and `persist_canonical_nodes`/`backfill_canonical_embeddings`
(PLAN_REVIEWED.md §10 Increment 11, §6.1/§6.2).

Own copy of `ps_service.domain_mapper.graph_writer`'s connectivity-wrapping
shape (PLAN_REVIEWED.md §0.3 -- a deliberate near-duplicate, not a shared
import): the `_execute_query` dependency-health wrapper, and the
`MERGE ... SET n += $properties` node-upsert shape for RegulatoryInstrument/Role/
Requirement, which are never canonically deduped (unconditional `SET`, same
as #15's own writer).

**The one deliberate, load-bearing difference from #15's writer (§6.1)**:
Capability node upserts use `MERGE (n:Capability {id: $id}) ON CREATE
SET n += $properties` -- NOT the unconditional `SET` used for Role/
Requirement/RegulatoryInstrument/Obligation above. `persist_canonical_nodes` is only
ever called for a `match_kind="new"` `CanonicalResolution` (an exact/
semantic match writes NOTHING onto the node it resolved to) -- but even so,
`ON CREATE SET` is what makes "an existing canonical node's properties are
never overwritten" a database-engine guarantee rather than application logic
remembering to check: an exact-key match means the incoming node's `id`
already equals an existing canonical node's `id`, and that incoming node's
`properties` dict comes from a *different* regulation's baseline graph and
could legitimately differ slightly. `ON CREATE SET` makes overwriting it
structurally impossible, regardless of what a caller passes.

**The second, new load-bearing difference (§6.2, B2's fix)**:
`backfill_canonical_embeddings` writes `MATCH (n:{kind} {id: $id}) WHERE
n.embedding IS NULL SET n.embedding = $embedding` for an ALREADY-EXISTING
canonical node whose embedding was computed during this run
(`dedup.dedupe_canonical_nodes`'s `DedupResult.embedding_backfills`) --
distinct from `persist_canonical_nodes`' `ON CREATE SET` mint path, and
deliberately never touching a node's other properties. The `WHERE
n.embedding IS NULL` guard is the mechanism, not an optimization: it makes a
re-run's backfill call against an already-backfilled node a structural
no-op at the database-engine level, the same load-bearing role `ON CREATE
SET` plays for the mint path above -- the caller (`merge.py`) does not need
to check first.

**Obligation is a passthrough node (issue #42)**: Obligation identity is
Role-scoped, so an Obligation is a weak entity of exactly one Role and is
never deduped across sources. `persist_obligation_passthrough` writes it
with the same unconditional `MERGE ... SET` shape used for Role/Requirement.
Only Capability (and, later, internal-SoP-derived Policy) is canonically
deduped by Company Merge.

**Edge rewiring (`persist_rewired_edges`, PLAN_REVIEWED.md §10 Increment 12,
§6.2)**: mirrors `domain_mapper.graph_writer._upsert_bare_edge`'s exact
`MATCH ... MERGE (s)-[:TYPE]->(t)` shape (no properties) for `HAS`/
`SATISFIED_BY`/`REQUIRES`. The rewrite rule is endpoint-agnostic, not
edge-type-specific: for BOTH the source and target of every edge, if that
endpoint's baseline-local id has an entry in `canonical_id_by_incoming_id`
(`incoming_id -> canonical_id`), it is rewritten to the canonical id;
otherwise it passes through unchanged. In practice only Capability endpoints
are ever in the mapping, so `REQUIRES`'s Capability-typed TARGET is the only
endpoint rewritten; `HAS`/`SATISFIED_BY` and `REQUIRES`'s Obligation-typed
source pass through unchanged (Obligation, Role, and Requirement are all
passthrough nodes whose baseline-local id is already their final id).

**Why `REQUIRES`'s target must be rewritten** (PLAN_REVIEWED.md §6.2): a
semantically-or-exactly-matched Capability is never minted as its own graph
node (`persist_canonical_nodes` only writes a node for `match_kind="new"`),
so leaving the baseline-local id in place would make `MATCH (s...),(t...)`
match zero rows for `t` -- the whole `MERGE` silently never fires and the
edge is never created: a dangling reference. The endpoint-agnostic rewrite
handles this without special-casing by relationship type.

`canonical_id_by_incoming_id` is a `dict[str, str]` the caller (`merge.py`)
builds from Capability dedup's `CanonicalResolution.incoming_id ->
canonical_id` entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import redis.exceptions

from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle, GraphQueryResult
    from ps_service.company_merge.models import (
        BareEdge,
        BaselineNode,
        CanonicalNodeProperties,
        CanonicalResolution,
        ProvenanceEdge,
    )

__all__ = [
    "backfill_canonical_embeddings",
    "persist_canonical_nodes",
    "persist_obligation_passthrough",
    "persist_rewired_edges",
    "persist_role_and_requirement_passthrough",
]

_REGULATORY_INSTRUMENT_LABEL = "RegulatoryInstrument"
_ROLE_LABEL = "Role"
_REQUIREMENT_LABEL = "Requirement"
_OBLIGATION_LABEL = "Obligation"
_CAPABILITY_LABEL = "Capability"

# source_label, target_label per relationship_type -- the Edge Catalog shape
# mirrored from domain_mapper.graph_writer.persist_obligation_and_capability_graph.
_EDGE_ENDPOINT_LABELS: dict[Literal["HAS", "SATISFIED_BY", "REQUIRES"], tuple[str, str]] = {
    "HAS": (_ROLE_LABEL, _OBLIGATION_LABEL),
    "SATISFIED_BY": (_REQUIREMENT_LABEL, _OBLIGATION_LABEL),
    "REQUIRES": (_OBLIGATION_LABEL, _CAPABILITY_LABEL),
}


def _execute_query(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> GraphQueryResult:
    """Wrap every `graph.query()` write in this module for connectivity-health recording.

    The one call site every write goes through, so FalkorDB connectivity
    failures get recorded in `ps_service.dependency_health` -- mirrors
    `ps_service.domain_mapper.graph_writer._execute_query` exactly.
    """
    try:
        result = graph.query(query, params=params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        raise CompanyMergePersistenceError(f"FalkorDB write failed: {exc}") from exc
    mark_healthy(FALKORDB)
    return result


def _upsert_passthrough_node(
    graph: GraphHandle, label: str, node_id: str, properties: dict[str, str | float]
) -> None:
    # label is always one of this module's own fixed literals (_ROLE_LABEL/
    # _REQUIREMENT_LABEL) -- never adapter/LLM-sourced -- mirrors
    # domain_mapper.graph_writer's own "no allow-list needed" design note.
    _execute_query(
        graph,
        f"MERGE (n:{label} {{id: $id}}) SET n += $properties",
        params={"id": node_id, "properties": properties},
    )


def _upsert_provenance_edge(
    graph: GraphHandle,
    relationship_type: Literal["DEFINES", "EXPRESSES"],
    target_label: str,
    regulatory_instrument_id: str,
    target_id: str,
    source_ref: str,
) -> None:
    """Write a `RegulatoryInstrument -[:relationship_type {source_ref}]-> target_label` edge.

    Shared shape for both `DEFINES` (Role) and `EXPRESSES` (Requirement)
    edges -- mirrors
    `domain_mapper.graph_writer._upsert_regulatory_instrument_edge` exactly.
    `relationship_type`/`target_label` are always fixed Python literals;
    `target_id`/`source_ref` flow through `params` only.
    """
    _execute_query(
        graph,
        f"MATCH (r:{_REGULATORY_INSTRUMENT_LABEL} {{id: $regulatory_instrument_id}}), "
        f"(n:{target_label} {{id: $target_id}}) "
        f"MERGE (r)-[e:{relationship_type}]->(n) SET e.source_ref = $source_ref",
        params={
            "regulatory_instrument_id": regulatory_instrument_id,
            "target_id": target_id,
            "source_ref": source_ref,
        },
    )


def persist_role_and_requirement_passthrough(
    single_tenant_graph: GraphHandle,
    regulatory_instrument_id: str,
    regulatory_instrument_properties: dict[str, object],
    role_nodes: tuple[BaselineNode, ...],
    requirement_nodes: tuple[BaselineNode, ...],
    provenance_edges: tuple[ProvenanceEdge, ...],
) -> None:
    """Persist one regulation's RegulatoryInstrument/Role/Requirement nodes and provenance edges.

    Writes the `DEFINES`/`EXPRESSES` edges into `single_tenant_graph` with an
    unconditional `MERGE ... SET`, mirroring #15's own
    `persist_role_and_requirement_graph` shape exactly (PLAN_REVIEWED.md §6).
    These node kinds are never canonically deduped -- RegulatoryInstrument has
    exactly one node per regulation, and Role/Requirement dedup is out of
    scope (AC-008) -- so there is no "existing wins" concern here, unlike
    Capability (`persist_canonical_nodes`, below). Obligation is in the same
    passthrough category since #42 -- see `persist_obligation_passthrough`.

    Idempotent: re-running with identical input against the same graph issues
    the same calls and leaves the same end state, since every write is a
    `MERGE` keyed on `id`.

    All nodes (RegulatoryInstrument, then every Role, then every Requirement)
    are written before any edge -- an edge write `MATCH`es both its
    RegulatoryInstrument and target endpoints, so writing nodes first is
    load-bearing, not stylistic, mirroring `domain_mapper.graph_writer`'s own
    write-order contract.
    """
    _execute_query(
        single_tenant_graph,
        f"MERGE (n:{_REGULATORY_INSTRUMENT_LABEL} {{id: $id}}) SET n += $properties",
        params={"id": regulatory_instrument_id, "properties": regulatory_instrument_properties},
    )
    for role in role_nodes:
        _upsert_passthrough_node(single_tenant_graph, _ROLE_LABEL, role.id, role.properties)
    for requirement in requirement_nodes:
        _upsert_passthrough_node(
            single_tenant_graph, _REQUIREMENT_LABEL, requirement.id, requirement.properties
        )
    for edge in provenance_edges:
        target_label = _ROLE_LABEL if edge.relationship_type == "DEFINES" else _REQUIREMENT_LABEL
        _upsert_provenance_edge(
            single_tenant_graph,
            edge.relationship_type,
            target_label,
            regulatory_instrument_id,
            edge.target_id,
            edge.source_ref,
        )


def persist_obligation_passthrough(
    single_tenant_graph: GraphHandle,
    obligation_nodes: tuple[BaselineNode, ...],
) -> None:
    """Persist one regulation's Obligation nodes into `single_tenant_graph`.

    Uses the same unconditional `MERGE ... SET` shape as Role/Requirement
    (issue #42: Obligation is Role-scoped, a weak entity of exactly one Role,
    never deduped across sources). No `ON CREATE SET` "existing wins" concern
    -- a given Obligation id can only ever originate from one Role, which
    originates from one regulation, so re-merging the same regulation is the
    only way the same id recurs and its properties are identical.

    Called by `merge.py` after `persist_role_and_requirement_passthrough`
    and before `persist_rewired_edges`, so the `HAS`/`SATISFIED_BY`/
    `REQUIRES` edge writes can `MATCH` these nodes.
    """
    for obligation in obligation_nodes:
        _upsert_passthrough_node(
            single_tenant_graph, _OBLIGATION_LABEL, obligation.id, obligation.properties
        )


def persist_canonical_nodes(
    single_tenant_graph: GraphHandle,
    incoming_nodes: tuple[BaselineNode, ...],
    resolutions: tuple[CanonicalResolution, ...],
    *,
    kind: Literal["Capability"],
) -> None:
    """Mint every `match_kind="new"` `CanonicalResolution` as a `kind` node.

    Writes into `single_tenant_graph` (PLAN_REVIEWED.md §6.1, Increment 11's
    mint half). A `match_kind="exact"`/`"semantic"` resolution gets NO write
    call at all here -- it already resolved onto an existing canonical node,
    and this function's whole point is to never touch one.

    `kind` is `"Capability"` only since #42 -- Obligation is no longer
    canonically deduped (`persist_obligation_passthrough` above); the
    parameter is kept for symmetry with a future internal-SoP Policy pass.

    `MERGE (n:{kind} {id: $id}) ON CREATE SET n += $properties` -- NOT an
    unconditional `SET` -- is the load-bearing invariant that makes
    "existing canonical node's properties are never overwritten" a
    database-engine guarantee (see module docstring).

    `$properties` is the underlying `BaselineNode.properties` (looked up
    from `incoming_nodes` by `resolution.incoming_id`) merged with
    `{"embedding": list(resolution.embedding)}` ONLY when
    `resolution.embedding is not None` -- a mint that never triggered any
    comparison (an empty working index the first time it fired, §5.4) has
    `embedding=None` and gets no `embedding` key at all; that node's
    embedding is filled in later, either later in the same run or via a
    future run's `backfill_canonical_embeddings` call.

    `incoming_nodes`/`resolutions` are guaranteed 1:1 by construction
    (`dedup.dedupe_canonical_nodes` builds exactly one `CanonicalResolution`
    per incoming node, `incoming_id` set to that node's own `id`) -- no
    validate-then-write pass, mirroring `domain_mapper.graph_writer.
    persist_obligation_and_capability_graph`'s own "correct by construction,
    no defensive re-check" reasoning.
    """
    nodes_by_id = {node.id: node for node in incoming_nodes}
    for resolution in resolutions:
        if resolution.match_kind != "new":
            continue
        node = nodes_by_id[resolution.incoming_id]
        properties: CanonicalNodeProperties = dict(node.properties)
        if resolution.embedding is not None:
            properties["embedding"] = list(resolution.embedding)
        _execute_query(
            single_tenant_graph,
            f"MERGE (n:{kind} {{id: $id}}) ON CREATE SET n += $properties",
            params={"id": resolution.canonical_id, "properties": properties},
        )


def backfill_canonical_embeddings(
    single_tenant_graph: GraphHandle,
    *,
    kind: Literal["Capability"],
    embeddings: dict[str, tuple[float, ...]],
) -> None:
    """Write each `embeddings` entry onto an ALREADY-EXISTING `kind` canonical node.

    For every `(id, embedding)` pair (PLAN_REVIEWED.md §6.2, B2's fix) --
    distinct from `persist_canonical_nodes`' `ON CREATE SET` mint path, and
    deliberately never touching a node's other properties.

    One call per id:

        MATCH (n:{kind} {id: $id})
        WHERE n.embedding IS NULL
        SET n.embedding = $embedding

    The `WHERE n.embedding IS NULL` guard is the mechanism, not an
    optimization: it makes a re-run's backfill call against an
    already-backfilled node a structural no-op at the database-engine
    level, the same load-bearing role `ON CREATE SET` plays for the mint
    path above -- the caller (`merge.py`) does not need to check first.

    Every id passed here is guaranteed (by construction --
    `dedup.dedupe_canonical_nodes`' `original_existing_ids` filter, §5.4) to
    already exist as a graph node by the time this is called; `merge.py`
    calls `persist_canonical_nodes` and all edge writes before this.
    """
    for node_id, embedding in embeddings.items():
        _execute_query(
            single_tenant_graph,
            f"MATCH (n:{kind} {{id: $id}}) WHERE n.embedding IS NULL SET n.embedding = $embedding",
            params={"id": node_id, "embedding": list(embedding)},
        )


def _dedupe_eligible_endpoint_ids(edge: BareEdge) -> tuple[str, ...]:
    """Return `edge`'s endpoints whose label (per `_EDGE_ENDPOINT_LABELS`) is Capability.

    Capability endpoints are the only ones that went through
    `dedup.dedupe_canonical_nodes` and are therefore guaranteed, by
    construction, to carry an entry in a correctly-built
    `canonical_id_by_incoming_id`. Since #42, Obligation is a passthrough
    node (Role-scoped, never deduped), so `HAS`/`SATISFIED_BY` yield nothing
    (Role/Requirement source, Obligation target -- all passthrough) and
    `REQUIRES` yields only its target (Capability); its Obligation source
    passes through. Order in the returned tuple is source-before-target when
    both are eligible; callers should not otherwise rely on it.
    """
    source_label, target_label = _EDGE_ENDPOINT_LABELS[edge.relationship_type]
    dedupe_eligible_labels = (_CAPABILITY_LABEL,)
    ids: list[str] = []
    if source_label in dedupe_eligible_labels:
        ids.append(edge.source_id)
    if target_label in dedupe_eligible_labels:
        ids.append(edge.target_id)
    return tuple(ids)


def _validate_rewired_edge_endpoints(
    bare_edges: tuple[BareEdge, ...], canonical_id_by_incoming_id: dict[str, str]
) -> None:
    """Whole-collection validation pass over every edge's dedupe-eligible endpoints.

    Mirrors `domain_mapper.graph_writer._validate_role_references`'s B3
    shape: EVERY one of an edge's dedupe-eligible endpoints (see
    `_dedupe_eligible_endpoint_ids` -- since #42 that is only a `REQUIRES`
    edge's Capability target) must resolve within `canonical_id_by_incoming_id`
    before `persist_rewired_edges` issues a single `graph.query` call for ANY
    edge -- not interleaved validate-then-write per edge. Raises
    `CompanyMergePersistenceError` on the first violation found, with zero
    `graph.query` calls having been made by the time this raises.

    Every Capability id that appears as a `REQUIRES` target is guaranteed by
    construction to have gone through `dedup.dedupe_canonical_nodes` and
    therefore have a `CanonicalResolution` entry in a correctly-built
    `canonical_id_by_incoming_id`. An absent entry means the caller-supplied
    mapping itself is incomplete (e.g. `merge.py` failed to pass Capability
    dedup's results), not that the endpoint "hasn't been resolved yet" -- so
    it is a hard pre-write error, not a pass-through.
    """
    for edge in bare_edges:
        for incoming_id in _dedupe_eligible_endpoint_ids(edge):
            if incoming_id not in canonical_id_by_incoming_id:
                raise CompanyMergePersistenceError(
                    f"{edge.relationship_type} edge references incoming id "
                    f"{incoming_id!r}, which has no CanonicalResolution entry in "
                    "canonical_id_by_incoming_id"
                )


def persist_rewired_edges(
    single_tenant_graph: GraphHandle,
    bare_edges: tuple[BareEdge, ...],
    canonical_id_by_incoming_id: dict[str, str],
) -> None:
    """Persist `HAS`/`SATISFIED_BY`/`REQUIRES` edges into `single_tenant_graph`.

    Rewrites each edge's endpoints through `canonical_id_by_incoming_id`
    (`incoming_id -> canonical_id`, Capability dedup's resolutions -- see
    module docstring) instead of writing the baseline-local id verbatim
    (PLAN_REVIEWED.md §6.2/§10 Increment 12).

    The rule is endpoint-agnostic, not edge-type-specific: for BOTH the
    source and target of every edge, if that endpoint's baseline-local id
    has an entry in `canonical_id_by_incoming_id`, it is rewritten to the
    canonical id; otherwise it passes through unchanged. Concretely, since
    #42 (Obligation is a passthrough node):

    - `HAS` (Role -[:HAS]-> Obligation): both endpoints pass through
      unchanged -- neither Role nor Obligation is ever in the mapping.
    - `SATISFIED_BY` (Requirement -[:SATISFIED_BY]-> Obligation): both
      endpoints pass through unchanged.
    - `REQUIRES` (Obligation -[:REQUIRES]-> Capability): source (Obligation)
      passes through, target (Capability) is rewritten -- Capability is the
      only canonically deduped endpoint. Its mapping entry is *required*
      (see `_validate_rewired_edge_endpoints`/`_dedupe_eligible_endpoint_ids`)
      -- an absent entry is a hard pre-write error, not a pass-through.

    Mirrors `domain_mapper.graph_writer._upsert_bare_edge`'s exact
    `MATCH ... MERGE (s)-[:TYPE]->(t)` shape -- no properties, since `HAS`/
    `SATISFIED_BY`/`REQUIRES` never carry any (Edge Catalog).

    Raises `CompanyMergePersistenceError` (via `_validate_rewired_edge_endpoints`)
    before any write if a `REQUIRES` edge's Capability target has no entry
    in `canonical_id_by_incoming_id` -- validate-then-write over the whole
    collection, mirroring `domain_mapper.graph_writer`'s own B3 fix shape,
    so a bad edge set writes
    nothing at all rather than a partial graph.

    Idempotent: re-running with the identical `bare_edges`/
    `canonical_id_by_incoming_id` against the same graph issues the same
    calls and leaves the same end state, since every write is a `MERGE`
    keyed on the same resolved `(source_id, relationship_type, target_id)`
    triple both times.
    """
    _validate_rewired_edge_endpoints(bare_edges, canonical_id_by_incoming_id)
    for edge in bare_edges:
        source_label, target_label = _EDGE_ENDPOINT_LABELS[edge.relationship_type]
        source_id = canonical_id_by_incoming_id.get(edge.source_id, edge.source_id)
        target_id = canonical_id_by_incoming_id.get(edge.target_id, edge.target_id)
        _execute_query(
            single_tenant_graph,
            f"MATCH (s:{source_label} {{id: $source_id}}), "
            f"(t:{target_label} {{id: $target_id}}) "
            f"MERGE (s)-[:{edge.relationship_type}]->(t)",
            params={"source_id": source_id, "target_id": target_id},
        )
