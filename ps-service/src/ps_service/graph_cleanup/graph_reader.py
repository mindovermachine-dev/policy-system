"""Read-only single-tenant graph reads for `ps_service.graph_cleanup`.

Every read is a literal, parameterised query with no write keyword. The connectivity-wrapping
shape matches `company_merge.pending_review._execute_query`: a driver failure marks FalkorDB
unhealthy and is re-raised as a generic `GraphCleanupPersistenceError`; success marks it
healthy. The planners validate; these reads only report what the graph holds, reporting a
missing node as `None` or absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.graph_cleanup.capability_status import active_capability_status
from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupValidationError,
)
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    CapabilityRecord,
    CapabilityResolution,
    CapabilityUnmergeInputs,
    CapabilityUnmergeState,
    EdgeRecord,
    GoverningPolicy,
    MergeState,
    ObligationMergeState,
    ObligationNodeState,
    ObligationRecord,
    ObligationUnmergeInputs,
    ObligationUnmergeState,
    PendingReviewRef,
    PropertyValue,
    ReleaseState,
    RequirementSourceRef,
    ResolutionTerminal,
    RoleDefines,
    RoleMergeState,
    RoleRef,
    RoleState,
    RoleUnmergeInputs,
    RoleUnmergeState,
    SplitMarkers,
    SplitTerminalState,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from ps_service.company_merge.falkordb_client import GraphHandle

__all__ = [
    "query_rows",
    "read_active_capabilities",
    "read_capability_merge_state",
    "read_capability_merged",
    "read_capability_present",
    "read_capability_present_without_marker",
    "read_capability_resolutions",
    "read_capability_split_markers",
    "read_capability_unmerge_state",
    "read_existing_sources",
    "read_governed_by_edge_present",
    "read_obligation_marker",
    "read_obligation_marker_present",
    "read_obligation_merge_markers",
    "read_obligation_merge_state",
    "read_obligation_present",
    "read_obligation_unmerge_state",
    "read_obligations_by_role",
    "read_pending_review",
    "read_release_state",
    "read_resolution_terminals",
    "read_resolved_capability_present",
    "read_role_merge_state",
    "read_role_merged",
    "read_role_present_without_marker",
    "read_role_unmerge_state",
    "read_split_terminal",
]

# `deprecated` Capabilities are not merge candidates; a node with no `status` property counts as
# active.
_ACTIVE_CAPABILITIES_QUERY = (
    "MATCH (c:Capability) WHERE c.status IS NULL OR c.status <> 'deprecated' "
    "OPTIONAL MATCH (c)-[:GOVERNED_BY]->(p:Policy) "
    "OPTIONAL MATCH (o:Obligation)-[:REQUIRES]->(c) "
    "RETURN c.id, c.name, c.embedding, p.id, p.title, p.status, count(DISTINCT o)"
)


# One row per (Obligation, Requirement); the Role is the Obligation's single inbound `HAS`.
# `source_ref` lives on the `EXPRESSES` edge, reached through `SATISFIED_BY`. `$role_id` NULL
# means every Role.
_OBLIGATIONS_BY_ROLE_QUERY = (
    "MATCH (r:Role)-[:HAS]->(o:Obligation) "
    "WHERE $role_id IS NULL OR r.id = $role_id "
    "OPTIONAL MATCH (q:Requirement)-[:SATISFIED_BY]->(o) "
    "OPTIONAL MATCH (:RegulatoryInstrument)-[e:EXPRESSES]->(q) "
    "RETURN r.id, r.name, o.id, o.text, q.id, e.source_ref"
)


def query_rows(
    graph: GraphHandle, query: str, params: dict[str, object] | None = None
) -> list[list[object]]:
    """Run one literal query, marking FalkorDB health; a driver failure is a generic error.

    Args:
        graph: Handle for the policy graph.
        query: Literal Cypher text; values go in `params`, never into the text.
        params: Query parameters; `None` runs the query without any.

    Returns:
        The result set, one list of column values per row; empty when nothing matched.

    Side Effects:
        Marks the FalkorDB dependency healthy on success and unhealthy on a driver failure.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    try:
        result = graph.query(query) if params is None else graph.query(query, params)
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        message = "the policy graph database could not be read"
        raise GraphCleanupPersistenceError(message) from exc
    mark_healthy(FALKORDB)
    return cast("list[list[object]]", result.result_set)


def read_obligations_by_role(
    graph: GraphHandle, *, role_id: str | None = None
) -> tuple[ObligationRecord, ...]:
    """Read every Obligation with its Role and its Requirements' `source_ref`s, a pure read.

    `role_id` limits the read to one Role. An Obligation with no live `SATISFIED_BY`
    edge (unprovenanced) is returned with no requirements.

    Args:
        graph: Handle for the policy graph.
        role_id: Role to read; `None` reads every Role.

    Returns:
        One record per Obligation, in query order; empty when no Obligation matches.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _OBLIGATIONS_BY_ROLE_QUERY, {"role_id": role_id})
    heads: dict[str, tuple[str, str, str]] = {}
    refs: dict[str, dict[str, str | None]] = {}
    for row in rows:
        obligation_id = cast("str", row[2])
        heads.setdefault(
            obligation_id, (cast("str", row[0]), cast("str", row[1]), cast("str", row[3]))
        )
        bucket = refs.setdefault(obligation_id, {})
        if row[4] is not None:
            bucket[cast("str", row[4])] = cast("str | None", row[5])
    return tuple(
        ObligationRecord(
            id=obligation_id,
            text=text,
            role_id=role,
            role_name=role_name,
            requirements=tuple(
                RequirementSourceRef(requirement_id=req, source_ref=ref)
                for req, ref in sorted(refs[obligation_id].items())
            ),
        )
        for obligation_id, (role, role_name, text) in heads.items()
    )


def read_active_capabilities(graph: GraphHandle) -> tuple[CapabilityRecord, ...]:
    """Read every active Capability, a pure read.

    Each record carries id, name, cached embedding, governing Policy (id, title,
    status) or `None`, and the count of Obligations that `REQUIRES` it.
    `embedding` is `None` for a Capability whose embedding was never cached;
    nothing is fetched or computed here. A Capability reported on several rows
    (more than one `GOVERNED_BY` edge, which the model forbids) appears once, with
    its first row's policy.

    Args:
        graph: Handle for the policy graph.

    Returns:
        One record per active Capability; empty when there is none.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _ACTIVE_CAPABILITIES_QUERY)
    records: dict[str, CapabilityRecord] = {}
    for row in rows:
        capability_id = cast("str", row[0])
        if capability_id in records:
            continue
        policy = (
            GoverningPolicy(
                id=cast("str", row[3]), title=cast("str", row[4]), status=cast("str", row[5])
            )
            if row[3] is not None
            else None
        )
        records[capability_id] = CapabilityRecord(
            id=capability_id,
            name=cast("str", row[1]),
            embedding=tuple(cast("list[float]", row[2])) if row[2] is not None else None,
            governing_policy=policy,
            obligation_count=cast("int", row[6]),
        )
    return tuple(records.values())


_RESOLVED_CAPABILITY_LABEL = "ResolvedCapability"
# The terminal of a resolution is read with its cached embedding, which the list compares with X's
# baseline embedding; the merge-state reads below never read it.
_CAPABILITY_EMBEDDINGS_QUERY = (
    "MATCH (c:Capability) WHERE c.id IN $ids RETURN c.id, c.name, c.embedding"
)


# Merge-state reads: one literal, parameterised read per concern. Property names are the
# scalar Capability properties of the Domain Concepts document; `embedding` is deliberately
# not read (it is large, derived, and never part of an audit snapshot).
_CAPABILITY_NODES_QUERY = (
    "MATCH (c:Capability) WHERE c.id IN $ids "
    "RETURN c.id, c.name, c.status, c.description, c.type, c.confidence, "
    "c.created_at"
)
_CAPABILITY_GOVERNORS_QUERY = (
    "MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy) WHERE c.id IN $ids "
    "RETURN c.id, p.id, p.title, p.status"
)
_POLICY_GOVERNED_SETS_QUERY = (
    "MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy) WHERE p.id IN $policy_ids RETURN p.id, c.id"
)
_INCOMING_EDGE_QUERIES: tuple[tuple[str, str, str], ...] = (
    (
        "REQUIRES",
        "Obligation",
        "MATCH (x:Obligation)-[:REQUIRES]->(c:Capability) WHERE c.id IN $ids RETURN x.id, c.id",
    ),
    (
        "COVERS",
        "PracticeArea",
        "MATCH (x:PracticeArea)-[:COVERS]->(c:Capability) WHERE c.id IN $ids RETURN x.id, c.id",
    ),
    (
        "MITIGATED_BY",
        "RiskPath",
        "MATCH (x:RiskPath)-[:MITIGATED_BY]->(c:Capability) WHERE c.id IN $ids RETURN x.id, c.id",
    ),
)
_CAPABILITY_MARKER_QUERY = (
    "MATCH (m:MergedCapability {id: $absorbed_id}) WHERE m.merged_into = $survivor_id "
    "RETURN count(m)"
)
_CAPABILITY_ANY_MARKER_QUERY = "MATCH (m:MergedCapability {id: $capability_id}) RETURN count(m)"
_CAPABILITY_PRESENT_QUERY = "MATCH (c:Capability {id: $capability_id}) RETURN count(c)"
_RESOLVED_CAPABILITY_PRESENT_QUERY = (
    "MATCH (m:ResolvedCapability {id: $capability_id}) RETURN count(m)"
)
_NODE_PROPERTY_NAMES = ("description", "type", "confidence", "created_at")


def _node_state(row: list[object]) -> CapabilityNodeState:
    """Build a Capability node state from a node-query row, skipping null properties."""
    properties: dict[str, PropertyValue] = {
        name: cast("PropertyValue", value)
        for name, value in zip(_NODE_PROPERTY_NAMES, row[3:7], strict=True)
        if value is not None
    }
    return CapabilityNodeState(
        id=cast("str", row[0]),
        name=cast("str", row[1]),
        status=cast("str | None", row[2]) or active_capability_status(),
        properties=properties,
    )


def read_capability_merge_state(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str
) -> MergeState:
    """Read both Capabilities, their in-scope incoming edges and their governing Policies.

    When either is governed, also reads every Capability each such Policy governs.
    A pure read: four to seven literal queries, no write keyword. A missing node is
    reported as `None` for that side; validation is the planner's job.

    Args:
        graph: Handle for the policy graph.
        survivor_id: Id of the Capability that would remain after the merge.
        absorbed_id: Id of the Capability that would be merged away.

    Returns:
        The merge state: both nodes (`None` when absent), incoming edges, governing Policies
        of each side and the governed set of every involved Policy.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": sorted({survivor_id, absorbed_id})}
    nodes = {
        cast("str", row[0]): _node_state(row)
        for row in query_rows(graph, _CAPABILITY_NODES_QUERY, params)
    }
    edges: list[EdgeRecord] = []
    for rel_type, source_label, query in _INCOMING_EDGE_QUERIES:
        edges.extend(
            EdgeRecord(
                rel_type=rel_type,
                source_label=source_label,
                source_id=cast("str", row[0]),
                target_label="Capability",
                target_id=cast("str", row[1]),
            )
            for row in query_rows(graph, query, params)
        )
    policies: dict[str, list[GoverningPolicy]] = {survivor_id: [], absorbed_id: []}
    for row in query_rows(graph, _CAPABILITY_GOVERNORS_QUERY, params):
        policies[cast("str", row[0])].append(
            GoverningPolicy(
                id=cast("str", row[1]), title=cast("str", row[2]), status=cast("str", row[3])
            )
        )
    policy_ids = sorted({p.id for governed in policies.values() for p in governed})
    governed_sets: dict[str, list[str]] = {}
    if policy_ids:
        for row in query_rows(graph, _POLICY_GOVERNED_SETS_QUERY, {"policy_ids": policy_ids}):
            governed_sets.setdefault(cast("str", row[0]), []).append(cast("str", row[1]))
    return MergeState(
        survivor=nodes.get(survivor_id),
        absorbed=nodes.get(absorbed_id),
        edges=tuple(edges),
        survivor_policies=tuple(policies[survivor_id]),
        absorbed_policies=tuple(policies[absorbed_id]),
        governed_sets={pid: tuple(sorted(ids)) for pid, ids in governed_sets.items()},
    )


def read_capability_merged(graph: GraphHandle, *, survivor_id: str, absorbed_id: str) -> bool:
    """Whether `absorbed_id` is merged into `survivor_id`: its marker exists and its node is gone.

    Args:
        graph: Handle for the policy graph.
        survivor_id: Id the `MergedCapability` marker must name in `merged_into`.
        absorbed_id: Id of the marker, and of the Capability that must no longer exist.

    Returns:
        True when such a marker exists and no Capability has the absorbed id, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(
        graph,
        _CAPABILITY_MARKER_QUERY,
        {"survivor_id": survivor_id, "absorbed_id": absorbed_id},
    )
    marked = bool(rows) and cast("int", rows[0][0]) > 0
    return marked and not _capability_present(graph, capability_id=absorbed_id)


def _capability_present(graph: GraphHandle, *, capability_id: str) -> bool:
    """Whether a `Capability` node with `capability_id` exists."""
    rows = query_rows(graph, _CAPABILITY_PRESENT_QUERY, {"capability_id": capability_id})
    return bool(rows) and cast("int", rows[0][0]) > 0


# Obligation-merge reads. An Obligation carries only `text` and `confidence` besides its id
# so its snapshot is read as scalar columns.
_OBLIGATION_NODES_QUERY = (
    "MATCH (o:Obligation) WHERE o.id IN $ids RETURN o.id, o.text, o.confidence"
)
_OBLIGATION_ROLES_QUERY = (
    "MATCH (r:Role)-[:HAS]->(o:Obligation) WHERE o.id IN $ids RETURN o.id, r.id, r.name"
)
_OBLIGATION_SATISFIED_QUERY = (
    "MATCH (q:Requirement)-[:SATISFIED_BY]->(o:Obligation) WHERE o.id IN $ids RETURN q.id, o.id"
)
_OBLIGATION_REQUIRES_QUERY = (
    "MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) WHERE o.id IN $ids RETURN o.id, c.id"
)
_OBLIGATION_REFS_QUERY = (
    "MATCH (q:Requirement)-[:SATISFIED_BY]->(o:Obligation {id: $obligation_id}) "
    "OPTIONAL MATCH (:RegulatoryInstrument)-[e:EXPRESSES]->(q) RETURN q.id, e.source_ref"
)
_OBLIGATION_MARKER_QUERY = (
    "MATCH (m:MergedObligation {id: $absorbed_id}) WHERE m.merged_into = $survivor_id "
    "RETURN count(m)"
)
_OBLIGATION_ANY_MARKER_QUERY = "MATCH (m:MergedObligation {id: $obligation_id}) RETURN count(m)"
_OBLIGATION_PRESENT_QUERY = "MATCH (o:Obligation {id: $obligation_id}) RETURN count(o)"


def _obligation_state(row: list[object]) -> ObligationNodeState:
    """Build an Obligation node state from an `_OBLIGATION_NODES_QUERY` row."""
    properties: dict[str, PropertyValue] = (
        {"confidence": cast("PropertyValue", row[2])} if row[2] is not None else {}
    )
    return ObligationNodeState(
        id=cast("str", row[0]), text=cast("str", row[1]), properties=properties
    )


def read_obligation_merge_state(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str
) -> ObligationMergeState:
    """Read both Obligations with their Roles, incident edges and `source_ref`s, a pure read.

    The edges are `SATISFIED_BY` / `REQUIRES`; the `source_ref`s are those of the absorbed
    side's Requirements.

    A missing node is reported as `None` for that side; validation is the planner's job.

    Args:
        graph: Handle for the policy graph.
        survivor_id: Id of the Obligation that would remain after the merge.
        absorbed_id: Id of the Obligation that would be merged away.

    Returns:
        The merge state: both nodes (`None` when absent), the Roles bearing each, their
        `SATISFIED_BY` / `REQUIRES` edges and the absorbed side's requirement source refs.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": sorted({survivor_id, absorbed_id})}
    nodes = {
        cast("str", row[0]): _obligation_state(row)
        for row in query_rows(graph, _OBLIGATION_NODES_QUERY, params)
    }
    roles: dict[str, list[RoleRef]] = {survivor_id: [], absorbed_id: []}
    for row in query_rows(graph, _OBLIGATION_ROLES_QUERY, params):
        roles[cast("str", row[0])].append(RoleRef(id=cast("str", row[1]), name=cast("str", row[2])))
    edges: list[EdgeRecord] = [
        EdgeRecord(
            rel_type="SATISFIED_BY",
            source_label="Requirement",
            source_id=cast("str", row[0]),
            target_label="Obligation",
            target_id=cast("str", row[1]),
        )
        for row in query_rows(graph, _OBLIGATION_SATISFIED_QUERY, params)
    ]
    edges.extend(
        EdgeRecord(
            rel_type="REQUIRES",
            source_label="Obligation",
            source_id=cast("str", row[0]),
            target_label="Capability",
            target_id=cast("str", row[1]),
        )
        for row in query_rows(graph, _OBLIGATION_REQUIRES_QUERY, params)
    )
    refs = query_rows(graph, _OBLIGATION_REFS_QUERY, {"obligation_id": absorbed_id})
    return ObligationMergeState(
        survivor=nodes.get(survivor_id),
        absorbed=nodes.get(absorbed_id),
        survivor_roles=tuple(roles[survivor_id]),
        absorbed_roles=tuple(roles[absorbed_id]),
        edges=tuple(edges),
        absorbed_requirement_refs=tuple(
            RequirementSourceRef(
                requirement_id=cast("str", row[0]), source_ref=cast("str | None", row[1])
            )
            for row in sorted(refs, key=lambda r: cast("str", r[0]))
        ),
    )


def read_obligation_marker(graph: GraphHandle, *, survivor_id: str, absorbed_id: str) -> bool:
    """Whether a `MergedObligation` marker redirects `absorbed_id` to `survivor_id`.

    Args:
        graph: Handle for the policy graph.
        survivor_id: Id the marker must name in `merged_into`.
        absorbed_id: Id of the marker.

    Returns:
        True when such a marker exists, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(
        graph,
        _OBLIGATION_MARKER_QUERY,
        {"survivor_id": survivor_id, "absorbed_id": absorbed_id},
    )
    return bool(rows) and cast("int", rows[0][0]) > 0


def read_obligation_marker_present(graph: GraphHandle, *, obligation_id: str) -> bool:
    """Whether any `MergedObligation` marker exists for `obligation_id`.

    Args:
        graph: Handle for the policy graph.
        obligation_id: Marker id to look for, whatever it redirects to.

    Returns:
        True when a marker exists, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _OBLIGATION_ANY_MARKER_QUERY, {"obligation_id": obligation_id})
    return bool(rows) and cast("int", rows[0][0]) > 0


def read_obligation_present(graph: GraphHandle, *, obligation_id: str) -> bool:
    """Whether an `Obligation` node with `obligation_id` exists.

    Args:
        graph: Handle for the policy graph.
        obligation_id: Id of the Obligation to look for.

    Returns:
        True when the node exists, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _OBLIGATION_PRESENT_QUERY, {"obligation_id": obligation_id})
    return bool(rows) and cast("int", rows[0][0]) > 0


# Role-merge reads. A Role carries `name`, `description` and `confidence` besides its id, so its
# snapshot is read as scalar columns. The edge reads are keyed by Role id; the degree read counts
# an edge of any type and direction, which catches an edge the merge does not know how to move.
_ROLE_NODES_QUERY = (
    "MATCH (r:Role) WHERE r.id IN $ids RETURN r.id, r.name, r.description, r.confidence"
)
_ROLE_MARKERS_QUERY = "MATCH (m:MergedRole) WHERE m.id IN $ids RETURN m.id, m.merged_into"
_ROLE_DEFINES_QUERY = (
    "MATCH (i:RegulatoryInstrument)-[d:DEFINES]->(r:Role) WHERE r.id IN $ids "
    "RETURN r.id, i.id, d.source_ref"
)
_ROLE_OBLIGATIONS_QUERY = (
    "MATCH (r:Role)-[:HAS]->(o:Obligation) WHERE r.id IN $ids RETURN r.id, o.id, o.text"
)
_ROLE_FOREIGN_BEARERS_QUERY = (
    "MATCH (a:Role {id: $absorbed_id})-[:HAS]->(o:Obligation)<-[:HAS]-(b:Role) "
    "WHERE b.id <> $absorbed_id RETURN o.id, b.id"
)
_ROLE_DEGREE_QUERY = "MATCH (r:Role {id: $role_id}) OPTIONAL MATCH (r)-[e]-() RETURN count(e)"


def _role_state(row: list[object]) -> RoleState:
    """Build a Role state from a `_ROLE_NODES_QUERY` row."""
    properties: dict[str, PropertyValue] = {
        key: cast("PropertyValue", value)
        for key, value in (("description", row[2]), ("confidence", row[3]))
        if value is not None
    }
    return RoleState(id=cast("str", row[0]), name=cast("str", row[1]), properties=properties)


def read_role_merge_state(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str
) -> RoleMergeState:
    """Read both Roles with their `DEFINES` and `HAS` edges and degrees, a pure read.

    A missing Role is reported as `None` for that side; validation is the planner's job.

    Args:
        graph: Handle for the policy graph.
        survivor_id: Id of the Role that would remain after the merge.
        absorbed_id: Id of the Role that would be merged away.

    Returns:
        The merge state: both Roles (`None` when absent), which ids have a `MergedRole` marker,
        each Role's defining instruments and borne Obligations, the absorbed Role's edge count,
        and any other Role bearing the absorbed Role's Obligations.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": sorted({survivor_id, absorbed_id})}
    nodes = {
        cast("str", row[0]): _role_state(row)
        for row in query_rows(graph, _ROLE_NODES_QUERY, params)
    }
    markers = tuple(
        sorted(cast("str", row[0]) for row in query_rows(graph, _ROLE_MARKERS_QUERY, params))
    )
    defines: dict[str, list[RoleDefines]] = {survivor_id: [], absorbed_id: []}
    for row in query_rows(graph, _ROLE_DEFINES_QUERY, params):
        defines[cast("str", row[0])].append(
            RoleDefines(instrument_id=cast("str", row[1]), source_ref=cast("str | None", row[2]))
        )
    obligations: dict[str, list[str]] = {survivor_id: [], absorbed_id: []}
    texts: dict[str, str] = {}
    for row in query_rows(graph, _ROLE_OBLIGATIONS_QUERY, params):
        obligations[cast("str", row[0])].append(cast("str", row[1]))
        if row[2] is not None:
            texts[cast("str", row[1])] = cast("str", row[2])
    degree_rows = query_rows(graph, _ROLE_DEGREE_QUERY, {"role_id": absorbed_id})
    foreign = tuple(
        EdgeRecord(
            rel_type="HAS",
            source_label="Role",
            source_id=cast("str", row[1]),
            target_label="Obligation",
            target_id=cast("str", row[0]),
        )
        for row in query_rows(graph, _ROLE_FOREIGN_BEARERS_QUERY, {"absorbed_id": absorbed_id})
    )
    return RoleMergeState(
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        survivor=nodes.get(survivor_id),
        absorbed=nodes.get(absorbed_id),
        marker_ids=markers,
        survivor_defines=tuple(defines[survivor_id]),
        absorbed_defines=tuple(defines[absorbed_id]),
        survivor_obligation_ids=tuple(sorted(obligations[survivor_id])),
        absorbed_obligation_ids=tuple(sorted(obligations[absorbed_id])),
        obligation_texts=texts,
        foreign_bearers=foreign,
        absorbed_degree=cast("int", degree_rows[0][0]) if degree_rows else 0,
    )


# A `PendingReview` is read raw, whatever its kind: Company Merge's own reader refuses a kind it has
# not registered, and a Role review is one of those until the Role extraction fix lands.
_PENDING_REVIEW_QUERY = (
    "MATCH (r:PendingReview {id: $review_id}) RETURN r.kind, r.incoming_id, r.nearest_existing_id"
)


def read_pending_review(graph: GraphHandle, *, review_id: str) -> PendingReviewRef | None:
    """Read one `PendingReview` by id with its kind and the pair it names, a pure read.

    Args:
        graph: Handle for the policy graph.
        review_id: Id of the review.

    Returns:
        The review's kind and ids; None when no review has that id (a resolved review is deleted).

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _PENDING_REVIEW_QUERY, {"review_id": review_id})
    if not rows:
        return None
    kind, incoming_id, nearest_id = rows[0]
    return PendingReviewRef(
        kind=str(kind), incoming_id=str(incoming_id), nearest_existing_id=str(nearest_id)
    )


def read_role_merged(graph: GraphHandle, *, survivor_id: str, absorbed_id: str) -> bool:
    """Whether `absorbed_id` is merged into `survivor_id`: its marker exists and its Role is gone.

    Args:
        graph: Handle for the policy graph.
        survivor_id: Id the `MergedRole` marker must name in `merged_into`.
        absorbed_id: Id of the marker, and of the Role that must no longer exist.

    Returns:
        True when such a marker exists and no Role has the absorbed id, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": [absorbed_id]}
    marked = any(row[1] == survivor_id for row in query_rows(graph, _ROLE_MARKERS_QUERY, params))
    return marked and not query_rows(graph, _ROLE_NODES_QUERY, params)


# Role-unmerge reads reuse the Role-merge node, marker and `HAS` reads, and the Obligation-merge
# read of an Obligation's bearing Roles; only the instrument's existence is new.
_INSTRUMENT_EXISTS_QUERY = "MATCH (i:RegulatoryInstrument {id: $instrument_id}) RETURN i.id"


def read_role_unmerge_state(graph: GraphHandle, *, inputs: RoleUnmergeInputs) -> RoleUnmergeState:
    """Read the live graph around the merged-away Role `inputs.absorbed_id`, a pure read.

    Args:
        graph: Handle for the policy graph.
        inputs: What the merge's audit row says to reverse: both ids, the instrument and the
            Obligations the merge moved.

    Returns:
        The live state: whether a Role with the absorbed id exists, the survivor (`None` when
        absent), the `MergedRole` marker targets of both ids, whether the instrument exists, the
        survivor's Obligations and every Role bearing each moved Obligation.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": sorted({inputs.survivor_id, inputs.absorbed_id})}
    nodes = {
        cast("str", row[0]): _role_state(row)
        for row in query_rows(graph, _ROLE_NODES_QUERY, params)
    }
    markers: dict[str, list[str]] = {}
    for row in query_rows(graph, _ROLE_MARKERS_QUERY, params):
        markers.setdefault(cast("str", row[0]), []).append(cast("str", row[1]))
    instrument_exists = bool(
        query_rows(graph, _INSTRUMENT_EXISTS_QUERY, {"instrument_id": inputs.instrument_id})
    )
    survivor_obligations = sorted(
        cast("str", row[1])
        for row in query_rows(graph, _ROLE_OBLIGATIONS_QUERY, {"ids": [inputs.survivor_id]})
    )
    bearers: dict[str, list[str]] = {}
    if inputs.moved_obligation_ids:
        for row in query_rows(
            graph, _OBLIGATION_ROLES_QUERY, {"ids": list(inputs.moved_obligation_ids)}
        ):
            bearers.setdefault(cast("str", row[0]), []).append(cast("str", row[1]))
    return RoleUnmergeState(
        absorbed_exists=inputs.absorbed_id in nodes,
        survivor=nodes.get(inputs.survivor_id),
        marker_targets={role: tuple(targets) for role, targets in markers.items()},
        instrument_exists=instrument_exists,
        survivor_obligation_ids=tuple(survivor_obligations),
        moved_bearers={obl: tuple(sorted(roles)) for obl, roles in bearers.items()},
    )


def read_role_present_without_marker(graph: GraphHandle, *, role_id: str) -> bool:
    """Whether `role_id` exists again as a Role and no `MergedRole` marker remains for it.

    Args:
        graph: Handle for the policy graph.
        role_id: Id of the previously absorbed Role.

    Returns:
        True when the Role exists and its marker is gone, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": [role_id]}
    return bool(query_rows(graph, _ROLE_NODES_QUERY, params)) and not query_rows(
        graph, _ROLE_MARKERS_QUERY, params
    )


# Release-governance reads: the Capability, its governing Policy and that Policy's governed set
# reuse the merge-state queries; the edge-presence read is the effect verifier's check.
_GOVERNED_EDGE_QUERY = (
    "MATCH (c:Capability {id: $capability_id})-[r:GOVERNED_BY]->(p:Policy {id: $policy_id}) "
    "RETURN count(r)"
)


def read_release_state(graph: GraphHandle, *, capability_id: str) -> ReleaseState:
    """Read one Capability, its governing Policies and the governed set of that Policy, a pure read.

    A missing Capability is reported as `None`; validation is the planner's job.

    Args:
        graph: Handle for the policy graph.
        capability_id: Id of the Capability to release.

    Returns:
        The release state: the Capability (`None` when absent), its governing Policies and the
        ids of every Capability the first governing Policy governs (empty when ungoverned).

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": [capability_id]}
    nodes = [
        _node_state(row)
        for row in query_rows(graph, _CAPABILITY_NODES_QUERY, params)
        if row[0] == capability_id
    ]
    policies = tuple(
        GoverningPolicy(
            id=cast("str", row[1]), title=cast("str", row[2]), status=cast("str", row[3])
        )
        for row in query_rows(graph, _CAPABILITY_GOVERNORS_QUERY, params)
        if row[0] == capability_id
    )
    governed_set: tuple[str, ...] = ()
    if policies:
        governed_set = tuple(
            sorted(
                {
                    cast("str", row[1])
                    for row in query_rows(
                        graph, _POLICY_GOVERNED_SETS_QUERY, {"policy_ids": [policies[0].id]}
                    )
                }
            )
        )
    return ReleaseState(
        capability=nodes[0] if nodes else None, policies=policies, governed_set=governed_set
    )


def read_governed_by_edge_present(
    graph: GraphHandle, *, capability_id: str, policy_id: str
) -> bool:
    """Whether `capability_id` is still `GOVERNED_BY` the Policy `policy_id`.

    Args:
        graph: Handle for the policy graph.
        capability_id: Id of the governed Capability.
        policy_id: Id of the governing Policy.

    Returns:
        True when the edge exists, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(
        graph, _GOVERNED_EDGE_QUERY, {"capability_id": capability_id, "policy_id": policy_id}
    )
    return bool(rows) and cast("int", rows[0][0]) > 0


# Capability-unmerge reads: the survivor and any node under the absorbed id, the
# `MergedCapability` markers of both ids, the survivor's current in-scope incoming edges and
# governing Policies (reusing the merge-state queries), and whether each snapshot endpoint and
# the restored Policy still exist.
_CAPABILITY_MARKERS_QUERY = (
    "MATCH (m:MergedCapability) WHERE m.id IN $ids RETURN m.id, m.merged_into"
)
_ENDPOINT_QUERIES: tuple[tuple[str, str], ...] = (
    ("REQUIRES", "MATCH (x:Obligation) WHERE x.id IN $ids RETURN x.id"),
    ("COVERS", "MATCH (x:PracticeArea) WHERE x.id IN $ids RETURN x.id"),
    ("MITIGATED_BY", "MATCH (x:RiskPath) WHERE x.id IN $ids RETURN x.id"),
)
_POLICY_EXISTS_QUERY = "MATCH (p:Policy {id: $policy_id}) RETURN p.id"


def read_capability_unmerge_state(
    graph: GraphHandle, *, inputs: CapabilityUnmergeInputs
) -> CapabilityUnmergeState:
    """Read the live graph around the marker `inputs.absorbed_id` for an unmerge, a pure read.

    A missing node is `None` (the survivor) or an absent status (the absorbed side); validation
    is the planner's job.

    Args:
        graph: Handle for the policy graph.
        inputs: What the merge's audit snapshot says to reverse: both ids and the edges and
            Policy to restore.

    Returns:
        The live state: the absorbed id's status (`None` when no node has it), the survivor
        (`None` when absent), the markers' targets, the survivor's incoming edges and governing
        Policy ids, which snapshot endpoints still exist, and whether the Policy to restore
        exists.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": sorted({inputs.survivor_id, inputs.absorbed_id})}
    nodes = {
        cast("str", row[0]): _node_state(row)
        for row in query_rows(graph, _CAPABILITY_NODES_QUERY, params)
    }
    markers: dict[str, list[str]] = {}
    for row in query_rows(graph, _CAPABILITY_MARKERS_QUERY, params):
        markers.setdefault(cast("str", row[0]), []).append(cast("str", row[1]))
    survivor_params: dict[str, object] = {"ids": [inputs.survivor_id]}
    survivor_edges = [
        EdgeRecord(
            rel_type=rel_type,
            source_label=source_label,
            source_id=cast("str", row[0]),
            target_label="Capability",
            target_id=inputs.survivor_id,
        )
        for rel_type, source_label, query in _INCOMING_EDGE_QUERIES
        for row in query_rows(graph, query, survivor_params)
        if row[1] == inputs.survivor_id
    ]
    survivor_policy_ids = tuple(
        sorted(
            cast("str", row[1])
            for row in query_rows(graph, _CAPABILITY_GOVERNORS_QUERY, survivor_params)
            if row[0] == inputs.survivor_id
        )
    )
    wanted = {
        "REQUIRES": inputs.restore_requires,
        "COVERS": inputs.restore_covers,
        "MITIGATED_BY": inputs.restore_mitigated,
    }
    existing: dict[str, tuple[str, ...]] = {}
    for rel_type, query in _ENDPOINT_QUERIES:
        ids = wanted[rel_type]
        found: set[str] = set()
        if ids:
            found = {cast("str", row[0]) for row in query_rows(graph, query, {"ids": list(ids)})}
        existing[rel_type] = tuple(sorted(found))
    policy_exists = inputs.restore_policy_id is None or bool(
        query_rows(graph, _POLICY_EXISTS_QUERY, {"policy_id": inputs.restore_policy_id})
    )
    absorbed = nodes.get(inputs.absorbed_id)
    return CapabilityUnmergeState(
        absorbed_status=absorbed.status if absorbed is not None else None,
        survivor=nodes.get(inputs.survivor_id),
        marker_targets={cap: tuple(targets) for cap, targets in markers.items()},
        survivor_edges=tuple(survivor_edges),
        survivor_policy_ids=survivor_policy_ids,
        existing_endpoints=existing,
        policy_exists=policy_exists,
    )


def read_capability_present_without_marker(graph: GraphHandle, *, capability_id: str) -> bool:
    """Whether `capability_id` exists again and no `MergedCapability` marker remains for it.

    Args:
        graph: Handle for the policy graph.
        capability_id: Id of the previously absorbed Capability.

    Returns:
        True when the Capability exists and its marker is gone, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _CAPABILITY_ANY_MARKER_QUERY, {"capability_id": capability_id})
    marked = bool(rows) and cast("int", rows[0][0]) > 0
    return not marked and _capability_present(graph, capability_id=capability_id)


# Obligation-unmerge reads: whether the absorbed id exists again, the `MergedObligation` markers of
# both ids, the survivor's current edges (reusing the obligation-merge edge queries) and whether
# the Role, every snapshot Requirement and every snapshot Capability still exist.
_OBLIGATION_MARKERS_QUERY = (
    "MATCH (m:MergedObligation) WHERE m.id IN $ids RETURN m.id, m.merged_into"
)
_ROLE_EXISTS_QUERY = "MATCH (r:Role {id: $role_id}) RETURN r.id"
_REQUIREMENTS_EXIST_QUERY = "MATCH (q:Requirement) WHERE q.id IN $ids RETURN q.id"
_CAPABILITIES_EXIST_QUERY = "MATCH (c:Capability) WHERE c.id IN $ids RETURN c.id"


def read_obligation_unmerge_state(
    graph: GraphHandle, *, inputs: ObligationUnmergeInputs
) -> ObligationUnmergeState:
    """Read the live graph around the deleted Obligation `inputs.absorbed_id`, a pure read.

    Args:
        graph: Handle for the policy graph.
        inputs: What the merge's audit snapshot says to reverse: both ids, the Role, and the
            Requirement and Capability ids to reattach.

    Returns:
        The live state: whether each Obligation exists, the survivor's text, `MergedObligation`
        marker targets, whether the Role exists, which snapshot Requirements exist, the status
        of each snapshot Capability and the survivor's current edges.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": sorted({inputs.survivor_id, inputs.absorbed_id})}
    nodes = {
        cast("str", row[0]): _obligation_state(row)
        for row in query_rows(graph, _OBLIGATION_NODES_QUERY, params)
    }
    markers: dict[str, list[str]] = {}
    for row in query_rows(graph, _OBLIGATION_MARKERS_QUERY, params):
        markers.setdefault(cast("str", row[0]), []).append(cast("str", row[1]))
    survivor_params: dict[str, object] = {"ids": [inputs.survivor_id]}
    survivor_edges = [
        EdgeRecord(
            rel_type="SATISFIED_BY",
            source_label="Requirement",
            source_id=cast("str", row[0]),
            target_label="Obligation",
            target_id=inputs.survivor_id,
        )
        for row in query_rows(graph, _OBLIGATION_SATISFIED_QUERY, survivor_params)
        if row[1] == inputs.survivor_id
    ]
    survivor_edges.extend(
        EdgeRecord(
            rel_type="REQUIRES",
            source_label="Obligation",
            source_id=inputs.survivor_id,
            target_label="Capability",
            target_id=cast("str", row[1]),
        )
        for row in query_rows(graph, _OBLIGATION_REQUIRES_QUERY, survivor_params)
        if row[0] == inputs.survivor_id
    )
    role_exists = bool(query_rows(graph, _ROLE_EXISTS_QUERY, {"role_id": inputs.role_id}))
    requirements: list[str] = []
    if inputs.satisfied_by_ids:
        requirements = sorted(
            cast("str", row[0])
            for row in query_rows(
                graph, _REQUIREMENTS_EXIST_QUERY, {"ids": list(inputs.satisfied_by_ids)}
            )
        )
    existing_capabilities: list[str] = []
    if inputs.requires_ids:
        existing_capabilities = sorted(
            cast("str", row[0])
            for row in query_rows(
                graph, _CAPABILITIES_EXIST_QUERY, {"ids": list(inputs.requires_ids)}
            )
        )
    gone_capabilities = [c for c in inputs.requires_ids if c not in existing_capabilities]
    merged_capability_targets: dict[str, list[str]] = {}
    if gone_capabilities:
        for row in query_rows(graph, _CAPABILITY_MARKERS_QUERY, {"ids": gone_capabilities}):
            merged_capability_targets.setdefault(cast("str", row[0]), []).append(
                cast("str", row[1])
            )
    survivor = nodes.get(inputs.survivor_id)
    return ObligationUnmergeState(
        absorbed_exists=inputs.absorbed_id in nodes,
        survivor_exists=survivor is not None,
        survivor_text=survivor.text if survivor is not None else "",
        marker_targets={obl: tuple(targets) for obl, targets in markers.items()},
        role_exists=role_exists,
        existing_requirements=tuple(requirements),
        existing_capabilities=tuple(existing_capabilities),
        merged_capability_targets={
            cap: tuple(targets) for cap, targets in merged_capability_targets.items()
        },
        survivor_edges=tuple(survivor_edges),
    )


def _read_markers_with_labels(
    graph: GraphHandle, kind: Literal["Capability", "Obligation"]
) -> tuple[dict[str, str], dict[str, str]]:
    """Company Merge's marker reads for `kind`, with its corrupt-graph and health handling.

    Raises:
        GraphCleanupValidationError: a marker has no target or an id is held by both labels.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    from ps_service.company_merge.errors import (  # noqa: PLC0415 -- function-local keeps graph_cleanup off Company Merge at import
        CompanyMergeValidationError,
    )
    from ps_service.company_merge.redirects import (  # noqa: PLC0415 -- function-local, as above
        read_markers_with_labels,
    )

    try:
        markers = read_markers_with_labels(graph, kind)
    except CompanyMergeValidationError as exc:
        raise GraphCleanupValidationError(str(exc)) from exc
    except redis.exceptions.RedisError as exc:
        mark_unhealthy(FALKORDB, error=exc)
        message = "the policy graph database could not be read"
        raise GraphCleanupPersistenceError(message) from exc
    mark_healthy(FALKORDB)
    return markers


def _capability_marker_chains(
    graph: GraphHandle,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Every Capability marker's target, label and chain terminal, by marked id.

    Raises:
        GraphCleanupValidationError: a marker has no target, an id is held by both labels, or a
            chain contains a cycle.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    from ps_service.company_merge.errors import (  # noqa: PLC0415 -- function-local keeps graph_cleanup off Company Merge at import
        CompanyMergeValidationError,
    )
    from ps_service.company_merge.redirects import (  # noqa: PLC0415 -- function-local, as above
        follow_markers,
    )

    targets, labels = _read_markers_with_labels(graph, "Capability")
    try:
        terminals = {marked: follow_markers(marked, targets, "Capability") for marked in targets}
    except CompanyMergeValidationError as exc:
        raise GraphCleanupValidationError(str(exc)) from exc
    return targets, labels, terminals


def _aliases(x_id: str, terminals: dict[str, str]) -> tuple[str, ...]:
    """The terminal of `x_id` and every other marked id whose chain ends there, sorted."""
    terminal = terminals[x_id]
    return tuple(
        sorted(
            {terminal}
            | {marked for marked, end in terminals.items() if end == terminal and marked != x_id}
        )
    )


def read_capability_resolutions(graph: GraphHandle) -> tuple[CapabilityResolution, ...]:
    """Read every `ResolvedCapability` marker and follow its chain to the terminal id.

    The markers and their chains are read and followed by Company Merge's own redirect reads, so a
    chain may pass through `MergedCapability` markers and the corrupt-graph checks are the ones
    every later ingest applies.

    Args:
        graph: Handle for the policy graph.

    Returns:
        One resolution per `ResolvedCapability`, sorted by its id, with the aliases of its
        terminal; empty when there is none. Whether the terminal exists is not checked here.

    Raises:
        GraphCleanupValidationError: a marker has no target, an id is held by both marker labels,
            or a chain contains a cycle.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    targets, labels, terminals = _capability_marker_chains(graph)
    return tuple(
        CapabilityResolution(
            x_id=x_id,
            marker_resolves_to=targets[x_id],
            a_id=terminals[x_id],
            a_aliases=_aliases(x_id, terminals),
        )
        for x_id in sorted(labels)
        if labels[x_id] == _RESOLVED_CAPABILITY_LABEL
    )


def read_capability_split_markers(graph: GraphHandle, x_id: str) -> SplitMarkers:
    """Read what the Capability markers say about `x_id`: its label, target, chain and aliases.

    Every Capability marker is read and every chain followed, so a corrupt chain anywhere refuses
    the split, as it refuses every later ingest.

    Args:
        graph: Handle for the policy graph.
        x_id: The id asked to be split.

    Returns:
        The marker label holding `x_id` and, when one does, its target, its chain to the terminal
        and the terminal's aliases; all empty when no marker holds `x_id`.

    Raises:
        GraphCleanupValidationError: a marker has no target, an id is held by both marker labels,
            or a chain contains a cycle.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    targets, labels, terminals = _capability_marker_chains(graph)
    if x_id not in labels:
        return SplitMarkers(x_marker_label=None, marker_resolves_to=None, chain=(), a_aliases=())
    chain = [x_id]
    while chain[-1] in targets:
        chain.append(targets[chain[-1]])
    return SplitMarkers(
        x_marker_label=labels[x_id],
        marker_resolves_to=targets[x_id],
        chain=tuple(chain),
        a_aliases=_aliases(x_id, terminals),
    )


def read_capability_present(graph: GraphHandle, *, capability_id: str) -> bool:
    """Whether a `Capability` node with `capability_id` exists.

    Args:
        graph: Handle for the policy graph.
        capability_id: The Capability id.

    Returns:
        True when the node exists, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _capability_present(graph, capability_id=capability_id)


def read_resolved_capability_present(graph: GraphHandle, *, capability_id: str) -> bool:
    """Whether a `ResolvedCapability` marker with `capability_id` exists.

    Args:
        graph: Handle for the policy graph.
        capability_id: The resolved Capability id.

    Returns:
        True when the marker exists, False otherwise.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _RESOLVED_CAPABILITY_PRESENT_QUERY, {"capability_id": capability_id})
    return bool(rows) and cast("int", rows[0][0]) > 0


def read_split_terminal(graph: GraphHandle, a_id: str) -> SplitTerminalState:
    """Read the terminal Capability of a split: its node, governing Policies and incoming edges.

    A pure read of the same literal queries the Capability merge state uses, for one id.

    Args:
        graph: Handle for the policy graph.
        a_id: The terminal Capability id.

    Returns:
        A's node (None when absent), the Policies governing it and every `REQUIRES` / `COVERS` /
        `MITIGATED_BY` edge into it.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": [a_id]}
    nodes = [_node_state(row) for row in query_rows(graph, _CAPABILITY_NODES_QUERY, params)]
    edges = [
        EdgeRecord(
            rel_type=rel_type,
            source_label=source_label,
            source_id=cast("str", row[0]),
            target_label="Capability",
            target_id=cast("str", row[1]),
        )
        for rel_type, source_label, query in _INCOMING_EDGE_QUERIES
        for row in query_rows(graph, query, params)
    ]
    policies = tuple(
        GoverningPolicy(
            id=cast("str", row[1]), title=cast("str", row[2]), status=cast("str", row[3])
        )
        for row in query_rows(graph, _CAPABILITY_GOVERNORS_QUERY, params)
    )
    return SplitTerminalState(
        node=nodes[0] if nodes else None, policies=policies, edges=tuple(edges)
    )


def read_obligation_merge_markers(graph: GraphHandle) -> dict[str, str]:
    """Read every `MergedObligation` marker as `{absorbed id: merged_into}`, chains not followed.

    Args:
        graph: Handle for the policy graph.

    Returns:
        The marker targets; empty when no Obligation was merged.

    Raises:
        GraphCleanupValidationError: a marker has no target.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    return _read_markers_with_labels(graph, "Obligation")[0]


def read_existing_sources(
    graph: GraphHandle, rel_type: str, ids: Collection[str]
) -> frozenset[str]:
    """Which of `ids` exist as nodes of the source label of the incoming edge type `rel_type`.

    Args:
        graph: Handle for the policy graph.
        rel_type: `REQUIRES` (Obligation), `COVERS` (PracticeArea) or `MITIGATED_BY` (RiskPath).
        ids: Candidate source ids.

    Returns:
        The ids that exist; empty when none does (no read is made when `ids` is empty).

    Raises:
        KeyError: `rel_type` is not an incoming Capability edge type.
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    query = dict(_ENDPOINT_QUERIES)[rel_type]
    if not ids:
        return frozenset()
    return frozenset(cast("str", row[0]) for row in query_rows(graph, query, {"ids": sorted(ids)}))


def read_resolution_terminals(
    graph: GraphHandle, ids: Collection[str]
) -> dict[str, ResolutionTerminal]:
    """Read the name, cached embedding and governing Policy of each terminal Capability.

    Args:
        graph: Handle for the policy graph.
        ids: Terminal Capability ids.

    Returns:
        The Capabilities that exist, by id; an id with no `Capability` node is absent. A
        Capability with several `GOVERNED_BY` edges (which the model forbids) reports the Policy
        with the lowest id.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    if not ids:
        return {}
    params: dict[str, object] = {"ids": sorted(ids)}
    nodes = {
        cast("str", row[0]): (
            cast("str", row[1]),
            tuple(cast("list[float]", row[2])) if row[2] is not None else None,
        )
        for row in query_rows(graph, _CAPABILITY_EMBEDDINGS_QUERY, params)
    }
    governors: dict[str, GoverningPolicy] = {}
    for row in query_rows(graph, _CAPABILITY_GOVERNORS_QUERY, params):
        policy = GoverningPolicy(
            id=cast("str", row[1]), title=cast("str", row[2]), status=cast("str", row[3])
        )
        current = governors.get(cast("str", row[0]))
        if current is None or policy.id < current.id:
            governors[cast("str", row[0])] = policy
    return {
        capability_id: ResolutionTerminal(
            id=capability_id,
            name=name,
            embedding=embedding,
            governing_policy=governors.get(capability_id),
        )
        for capability_id, (name, embedding) in nodes.items()
    }
