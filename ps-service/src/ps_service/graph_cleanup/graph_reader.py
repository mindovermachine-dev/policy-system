"""Read-only single-tenant graph reads for `ps_service.graph_cleanup` (issue #190).

Own copy of the connectivity-wrapping shape used by `company_merge.pending_review`
(`_execute_query`): a driver failure marks FalkorDB unhealthy and is re-raised as
a generic `GraphCleanupPersistenceError`; success marks it healthy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import redis.exceptions

from ps_service.dependency_health import FALKORDB, mark_healthy, mark_unhealthy
from ps_service.graph_cleanup.errors import GraphCleanupPersistenceError
from ps_service.graph_cleanup.models import (
    CapabilityNodeState,
    CapabilityRecord,
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
    PropertyValue,
    ReleaseState,
    RequirementSourceRef,
    RoleRef,
)

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle

__all__ = [
    "query_rows",
    "read_active_capabilities",
    "read_capability_merge_state",
    "read_capability_tombstone",
    "read_capability_unmerge_state",
    "read_capability_unmerged",
    "read_governed_by_edge_present",
    "read_obligation_marker",
    "read_obligation_marker_present",
    "read_obligation_merge_state",
    "read_obligation_present",
    "read_obligation_unmerge_state",
    "read_obligations_by_role",
    "read_release_state",
]

# `merged` tombstones and `deprecated` Capabilities are not merge candidates; a
# legacy node with no `status` counts as active (DC Capability lifecycle).
_ACTIVE_CAPABILITIES_QUERY = (
    "MATCH (c:Capability) WHERE coalesce(c.status,'active') = 'active' "
    "OPTIONAL MATCH (c)-[:GOVERNED_BY]->(p:Policy) "
    "OPTIONAL MATCH (o:Obligation)-[:REQUIRES]->(c) "
    "RETURN c.id, c.name, c.embedding, p.id, p.title, p.status, count(DISTINCT o)"
)


# One row per (Obligation, Requirement); the Role is the Obligation's single inbound `HAS`.
# `source_ref` lives on the `EXPRESSES` edge (DC provenance rule, case 2), reached through
# `SATISFIED_BY`. `$role_id` NULL means every Role.
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


# Merge-state reads: one literal, parameterised read per concern. Property names are the
# scalar Capability properties of the Domain Concepts document; `embedding` is deliberately
# not read (it is large, derived, and never part of an audit snapshot).
_CAPABILITY_NODES_QUERY = (
    "MATCH (c:Capability) WHERE c.id IN $ids "
    "RETURN c.id, c.name, coalesce(c.status,'active'), c.description, c.type, c.confidence"
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
_CAPABILITY_TOMBSTONE_QUERY = (
    "MATCH (a:Capability {id: $absorbed_id, status: 'merged'})"
    "-[:MERGED_INTO]->(s:Capability {id: $survivor_id}) RETURN count(a)"
)
_NODE_PROPERTY_NAMES = ("description", "type", "confidence")


def _node_state(row: list[object]) -> CapabilityNodeState:
    properties: dict[str, PropertyValue] = {
        name: cast("PropertyValue", value)
        for name, value in zip(_NODE_PROPERTY_NAMES, row[3:6], strict=True)
        if value is not None
    }
    return CapabilityNodeState(
        id=cast("str", row[0]),
        name=cast("str", row[1]),
        status=cast("str", row[2]),
        properties=properties,
    )


def read_capability_merge_state(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str
) -> MergeState:
    """Read both Capabilities, their in-scope incoming edges and their governing Policies.

    When either is governed, also reads every Capability each such Policy governs.
    A pure read: four to seven literal queries, no write keyword. A missing node is
    reported as `None` for that side; validation is the planner's job.

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


def read_capability_tombstone(graph: GraphHandle, *, survivor_id: str, absorbed_id: str) -> bool:
    """Whether `absorbed_id` is a `merged` tombstone with a `MERGED_INTO` edge to `survivor_id`.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(
        graph,
        _CAPABILITY_TOMBSTONE_QUERY,
        {"survivor_id": survivor_id, "absorbed_id": absorbed_id},
    )
    return bool(rows) and cast("int", rows[0][0]) > 0


# Obligation-merge reads. An Obligation carries only `text` and `confidence` besides its id
# (DC Obligation properties), so its snapshot is read as scalar columns.
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

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _OBLIGATION_ANY_MARKER_QUERY, {"obligation_id": obligation_id})
    return bool(rows) and cast("int", rows[0][0]) > 0


def read_obligation_present(graph: GraphHandle, *, obligation_id: str) -> bool:
    """Whether an `Obligation` node with `obligation_id` exists.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _OBLIGATION_PRESENT_QUERY, {"obligation_id": obligation_id})
    return bool(rows) and cast("int", rows[0][0]) > 0


# Release-governance reads: the Capability, its governing Policy and that Policy's governed set
# reuse the merge-state queries; the edge-presence read is the effect verifier's check.
_GOVERNED_EDGE_QUERY = (
    "MATCH (c:Capability {id: $capability_id})-[r:GOVERNED_BY]->(p:Policy {id: $policy_id}) "
    "RETURN count(r)"
)


def read_release_state(graph: GraphHandle, *, capability_id: str) -> ReleaseState:
    """Read one Capability, its governing Policies and the governed set of that Policy, a pure read.

    A missing Capability is reported as `None`; validation is the planner's job.

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

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(
        graph, _GOVERNED_EDGE_QUERY, {"capability_id": capability_id, "policy_id": policy_id}
    )
    return bool(rows) and cast("int", rows[0][0]) > 0


# Capability-unmerge reads: the tombstone and survivor, their live redirects, the survivor's
# current in-scope incoming edges and governing Policies (reusing the merge-state queries), and
# whether each snapshot endpoint and the restored Policy still exist.
_CAPABILITY_REDIRECTS_QUERY = (
    "MATCH (c:Capability)-[:MERGED_INTO]->(t:Capability) WHERE c.id IN $ids RETURN c.id, t.id"
)
_ENDPOINT_QUERIES: tuple[tuple[str, str], ...] = (
    ("REQUIRES", "MATCH (x:Obligation) WHERE x.id IN $ids RETURN x.id"),
    ("COVERS", "MATCH (x:PracticeArea) WHERE x.id IN $ids RETURN x.id"),
    ("MITIGATED_BY", "MATCH (x:RiskPath) WHERE x.id IN $ids RETURN x.id"),
)
_POLICY_EXISTS_QUERY = "MATCH (p:Policy {id: $policy_id}) RETURN p.id"
_CAPABILITY_UNMERGED_QUERY = (
    "MATCH (a:Capability {id: $absorbed_id}) WHERE coalesce(a.status,'active') = 'active' "
    "OPTIONAL MATCH (a)-[m:MERGED_INTO]->(:Capability) RETURN count(DISTINCT a), count(m)"
)


def read_capability_unmerge_state(
    graph: GraphHandle, *, inputs: CapabilityUnmergeInputs
) -> CapabilityUnmergeState:
    """Read the live graph around the tombstone `inputs.absorbed_id` for an unmerge, a pure read.

    A missing node is `None`; validation is the planner's job.

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    params: dict[str, object] = {"ids": sorted({inputs.survivor_id, inputs.absorbed_id})}
    nodes = {
        cast("str", row[0]): _node_state(row)
        for row in query_rows(graph, _CAPABILITY_NODES_QUERY, params)
    }
    redirects: dict[str, list[str]] = {}
    for row in query_rows(graph, _CAPABILITY_REDIRECTS_QUERY, params):
        redirects.setdefault(cast("str", row[0]), []).append(cast("str", row[1]))
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
    return CapabilityUnmergeState(
        absorbed=nodes.get(inputs.absorbed_id),
        survivor=nodes.get(inputs.survivor_id),
        redirects={cap: tuple(targets) for cap, targets in redirects.items()},
        survivor_edges=tuple(survivor_edges),
        survivor_policy_ids=survivor_policy_ids,
        existing_endpoints=existing,
        policy_exists=policy_exists,
    )


def read_capability_unmerged(graph: GraphHandle, *, capability_id: str) -> bool:
    """Whether `capability_id` is an active Capability with no `MERGED_INTO` edge (unmerged).

    Raises:
        GraphCleanupPersistenceError: the graph database could not be read.
    """
    rows = query_rows(graph, _CAPABILITY_UNMERGED_QUERY, {"absorbed_id": capability_id})
    return bool(rows) and cast("int", rows[0][0]) > 0 and cast("int", rows[0][1]) == 0


# Obligation-unmerge reads: whether the absorbed id exists again, the `MergedObligation` markers of
# both ids, the survivor's current edges (reusing the obligation-merge edge queries) and whether
# the Role, every snapshot Requirement and every snapshot Capability still exist.
_OBLIGATION_MARKERS_QUERY = (
    "MATCH (m:MergedObligation) WHERE m.id IN $ids RETURN m.id, m.merged_into"
)
_ROLE_EXISTS_QUERY = "MATCH (r:Role {id: $role_id}) RETURN r.id"
_REQUIREMENTS_EXIST_QUERY = "MATCH (q:Requirement) WHERE q.id IN $ids RETURN q.id"
_CAPABILITY_STATUSES_QUERY = (
    "MATCH (c:Capability) WHERE c.id IN $ids RETURN c.id, coalesce(c.status,'active')"
)


def read_obligation_unmerge_state(
    graph: GraphHandle, *, inputs: ObligationUnmergeInputs
) -> ObligationUnmergeState:
    """Read the live graph around the deleted Obligation `inputs.absorbed_id`, a pure read.

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
    statuses: dict[str, str] = {}
    if inputs.requires_ids:
        statuses = {
            cast("str", row[0]): cast("str", row[1])
            for row in query_rows(
                graph, _CAPABILITY_STATUSES_QUERY, {"ids": list(inputs.requires_ids)}
            )
        }
    survivor = nodes.get(inputs.survivor_id)
    return ObligationUnmergeState(
        absorbed_exists=inputs.absorbed_id in nodes,
        survivor_exists=survivor is not None,
        survivor_text=survivor.text if survivor is not None else "",
        marker_targets={obl: tuple(targets) for obl, targets in markers.items()},
        role_exists=role_exists,
        existing_requirements=tuple(requirements),
        capability_statuses=statuses,
        survivor_edges=tuple(survivor_edges),
    )
