"""Guarded single-statement graph writes for `ps_service.graph_cleanup` (issue #190).

Contract (CHANGES.md A1, I1-I5): one `graph.query` call; every `OPTIONAL MATCH` is
aggregated with `collect(DISTINCT ..)` in the next `WITH` so rows stay at one; no write
keyword precedes the guard `WHERE`; the guard pins node existence and status, per-edge-class
counts and the governing policy; only `FOREACH`/`SET`/`MERGE`/`DELETE` follow it; one row on
apply, zero rows on a guard miss. FalkorDB runs one statement atomically, so a failure
leaves the graph unchanged (AC-BI-018).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ps_service.graph_cleanup.errors import (
    GraphCleanupPersistenceError,
    GraphCleanupStaleStateError,
)
from ps_service.graph_cleanup.graph_reader import query_rows

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_cleanup.models import (
        CapabilityUnmergeWrite,
        ExpectedCounts,
        ObligationExpectedCounts,
        ObligationUnmergeWrite,
    )

__all__ = [
    "MERGE_CAPABILITIES_QUERY",
    "MERGE_OBLIGATIONS_QUERY",
    "RELEASE_GOVERNANCE_QUERY",
    "UNMERGE_CAPABILITY_QUERY",
    "UNMERGE_OBLIGATION_QUERY",
    "merge_capabilities",
    "merge_obligations",
    "release_governance",
    "unmerge_capability",
    "unmerge_obligation",
]

MERGE_CAPABILITIES_QUERY = """
MATCH (s:Capability {id: $survivor_id}), (a:Capability {id: $absorbed_id})
WHERE coalesce(s.status,'active') = 'active' AND coalesce(a.status,'active') = 'active'
OPTIONAL MATCH (o:Obligation)-[rq:REQUIRES]->(a)
WITH s, a, collect(DISTINCT o) AS req_src, collect(DISTINCT rq) AS req_rels
OPTIONAL MATCH (pa:PracticeArea)-[cv:COVERS]->(a)
WITH s, a, req_src, req_rels, collect(DISTINCT pa) AS cov_src, collect(DISTINCT cv) AS cov_rels
OPTIONAL MATCH (rp:RiskPath)-[mb:MITIGATED_BY]->(a)
WITH s, a, req_src, req_rels, cov_src, cov_rels,
     collect(DISTINCT rp) AS mit_src, collect(DISTINCT mb) AS mit_rels
OPTIONAL MATCH (a)-[ga:GOVERNED_BY]->(pa_pol:Policy)
WITH s, a, req_src, req_rels, cov_src, cov_rels, mit_src, mit_rels,
     collect(DISTINCT pa_pol) AS gov_a, collect(DISTINCT ga) AS gov_a_rels
OPTIONAL MATCH (s)-[:GOVERNED_BY]->(ps_pol:Policy)
WITH s, a, req_src, req_rels, cov_src, cov_rels, mit_src, mit_rels, gov_a, gov_a_rels,
     collect(DISTINCT ps_pol) AS gov_s
WHERE size(req_rels) = $expected_requires AND size(cov_rels) = $expected_covers
  AND size(mit_rels) = $expected_mitigated
  AND size(gov_a) = $expected_absorbed_governed AND size(gov_s) = $expected_survivor_governed
  AND all(p IN gov_a WHERE p.id = $expected_absorbed_policy_id
      AND p.status = $expected_absorbed_policy_status)
  AND all(p IN gov_s WHERE p.id = $expected_survivor_policy_id
      AND p.status = $expected_survivor_policy_status)
  AND NOT (size(gov_a) = 1 AND size(gov_s) = 1 AND gov_a[0].id <> gov_s[0].id)
FOREACH (x IN req_src | MERGE (x)-[:REQUIRES]->(s))
FOREACH (x IN cov_src | MERGE (x)-[:COVERS]->(s))
FOREACH (x IN mit_src | MERGE (x)-[:MITIGATED_BY]->(s))
FOREACH (p IN CASE WHEN size(gov_s) = 0 THEN gov_a ELSE [] END | MERGE (s)-[:GOVERNED_BY]->(p))
FOREACH (r IN req_rels | DELETE r)
FOREACH (r IN cov_rels | DELETE r)
FOREACH (r IN mit_rels | DELETE r)
FOREACH (r IN gov_a_rels | DELETE r)
SET a.status = 'merged'
MERGE (a)-[:MERGED_INTO]->(s)
RETURN s.id AS survivor_id, a.id AS absorbed_id
"""


def merge_capabilities(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str, expected: ExpectedCounts
) -> None:
    """Tombstone `absorbed_id` into `survivor_id` in one guarded, all-or-nothing statement.

    Raises:
        GraphCleanupStaleStateError: the guard did not match (a node, its status, an
            edge count or its governing policy differs from `expected`); nothing was written.
        GraphCleanupPersistenceError: the graph database failed; carries a generic
            message only, the driver error is chained.
    """
    params: dict[str, object] = {
        "survivor_id": survivor_id,
        "absorbed_id": absorbed_id,
        "expected_requires": expected.requires,
        "expected_covers": expected.covers,
        "expected_mitigated": expected.mitigated,
        "expected_absorbed_governed": expected.absorbed_governed,
        "expected_survivor_governed": expected.survivor_governed,
        "expected_absorbed_policy_id": expected.absorbed_policy_id,
        "expected_absorbed_policy_status": expected.absorbed_policy_status,
        "expected_survivor_policy_id": expected.survivor_policy_id,
        "expected_survivor_policy_status": expected.survivor_policy_status,
    }
    try:
        rows = query_rows(graph, MERGE_CAPABILITIES_QUERY, params)
    except GraphCleanupPersistenceError as exc:
        message = "the policy graph database could not complete the write"
        raise GraphCleanupPersistenceError(message) from exc
    if not rows:
        message = "the capabilities changed since the preview; nothing was written"
        raise GraphCleanupStaleStateError(message)


# CHANGES.md A2 (+ `s <> a`): the shared-Role match and the distinctness check are the
# AC-BI-015/016 guard in the statement itself (defence in depth; the preview rejects first).
# The marker is written in the SAME statement as the delete (H1), so a delete without a
# redirect is impossible.
MERGE_OBLIGATIONS_QUERY = """
MATCH (r:Role)-[:HAS]->(s:Obligation {id: $survivor_id}),
      (r)-[:HAS]->(a:Obligation {id: $absorbed_id})
OPTIONAL MATCH (q:Requirement)-[sb:SATISFIED_BY]->(a)
WITH s, a, collect(DISTINCT q) AS sat_src, collect(DISTINCT sb) AS sat_rels
OPTIONAL MATCH (a)-[rq:REQUIRES]->(c:Capability)
WITH s, a, sat_src, sat_rels, collect(DISTINCT c) AS req_tgt, collect(DISTINCT rq) AS req_rels
WHERE s <> a AND size(sat_rels) = $expected_satisfied AND size(req_rels) = $expected_requires
FOREACH (x IN sat_src | MERGE (x)-[:SATISFIED_BY]->(s))
FOREACH (c IN req_tgt | MERGE (s)-[:REQUIRES]->(c))
MERGE (m:MergedObligation {id: $absorbed_id})
SET m.merged_into = $survivor_id
DETACH DELETE a
RETURN $survivor_id AS survivor_id, $absorbed_id AS absorbed_id
"""


def merge_obligations(
    graph: GraphHandle,
    *,
    survivor_id: str,
    absorbed_id: str,
    expected: ObligationExpectedCounts,
) -> None:
    """Union `absorbed_id`'s edges onto `survivor_id` and delete it, in one guarded statement.

    Raises:
        GraphCleanupStaleStateError: the guard did not match (a node is gone, the pair no
            longer shares a Role, or an edge count differs from `expected`); nothing was
            written.
        GraphCleanupPersistenceError: the graph database failed; carries a generic
            message only, the driver error is chained.
    """
    params: dict[str, object] = {
        "survivor_id": survivor_id,
        "absorbed_id": absorbed_id,
        "expected_satisfied": expected.satisfied,
        "expected_requires": expected.requires,
    }
    try:
        rows = query_rows(graph, MERGE_OBLIGATIONS_QUERY, params)
    except GraphCleanupPersistenceError as exc:
        message = "the policy graph database could not complete the write"
        raise GraphCleanupPersistenceError(message) from exc
    if not rows:
        message = "the obligations changed since the preview; nothing was written"
        raise GraphCleanupStaleStateError(message)


# CHANGES.md A4: the draft-only guard is in the statement itself (defence in depth; the preview
# rejects first). Only the one edge is deleted; the Capability and the Policy stay untouched.
RELEASE_GOVERNANCE_QUERY = """
MATCH (c:Capability {id: $capability_id})
      -[r:GOVERNED_BY]->(p:Policy {id: $policy_id, status: 'draft'})
WHERE coalesce(c.status,'active') = 'active'
DELETE r
RETURN c.id AS capability_id
"""


def release_governance(graph: GraphHandle, *, capability_id: str, policy_id: str) -> None:
    """Delete the `GOVERNED_BY` edge from `capability_id` to the draft Policy `policy_id`.

    Raises:
        GraphCleanupStaleStateError: the guard did not match (the edge is gone, the Capability
            is no longer active or the Policy is no longer a draft); nothing was written.
        GraphCleanupPersistenceError: the graph database failed; carries a generic
            message only, the driver error is chained.
    """
    params: dict[str, object] = {"capability_id": capability_id, "policy_id": policy_id}
    try:
        rows = query_rows(graph, RELEASE_GOVERNANCE_QUERY, params)
    except GraphCleanupPersistenceError as exc:
        message = "the policy graph database could not complete the write"
        raise GraphCleanupPersistenceError(message) from exc
    if not rows:
        message = "the capability or its policy changed since the preview; nothing was written"
        raise GraphCleanupStaleStateError(message)


# CHANGES.md A5: reverses exactly the edges the merge moved. The tombstone, the survivor's
# status and every restore/remove count are pinned in the guard; edges the survivor gained
# since the merge are never matched, so they stay.
UNMERGE_CAPABILITY_QUERY = """
MATCH (a:Capability {id: $absorbed_id, status: 'merged'})
      -[m:MERGED_INTO]->(s:Capability {id: $survivor_id})
WHERE coalesce(s.status,'active') = 'active'
OPTIONAL MATCH (o:Obligation) WHERE o.id IN $restore_requires_ids
WITH a, m, s, collect(DISTINCT o) AS rq_nodes
OPTIONAL MATCH (x:Obligation)-[rx:REQUIRES]->(s) WHERE x.id IN $remove_requires_ids
WITH a, m, s, rq_nodes, collect(DISTINCT rx) AS rq_rm
OPTIONAL MATCH (pa:PracticeArea) WHERE pa.id IN $restore_covers_ids
WITH a, m, s, rq_nodes, rq_rm, collect(DISTINCT pa) AS cov_nodes
OPTIONAL MATCH (y:PracticeArea)-[ry:COVERS]->(s) WHERE y.id IN $remove_covers_ids
WITH a, m, s, rq_nodes, rq_rm, cov_nodes, collect(DISTINCT ry) AS cov_rm
OPTIONAL MATCH (rp:RiskPath) WHERE rp.id IN $restore_mitigated_ids
WITH a, m, s, rq_nodes, rq_rm, cov_nodes, cov_rm, collect(DISTINCT rp) AS mit_nodes
OPTIONAL MATCH (z:RiskPath)-[rz:MITIGATED_BY]->(s) WHERE z.id IN $remove_mitigated_ids
WITH a, m, s, rq_nodes, rq_rm, cov_nodes, cov_rm, mit_nodes, collect(DISTINCT rz) AS mit_rm
OPTIONAL MATCH (g:Policy {id: $restore_policy_id})
WITH a, m, s, rq_nodes, rq_rm, cov_nodes, cov_rm, mit_nodes, mit_rm, collect(DISTINCT g) AS pol
OPTIONAL MATCH (s)-[sg:GOVERNED_BY]->(:Policy {id: $restore_policy_id})
WITH a, m, s, rq_nodes, rq_rm, cov_nodes, cov_rm, mit_nodes, mit_rm, pol,
     collect(DISTINCT sg) AS gov_rm
WHERE size(rq_nodes) = size($restore_requires_ids) AND size(rq_rm) = size($remove_requires_ids)
  AND size(cov_nodes) = size($restore_covers_ids) AND size(cov_rm) = size($remove_covers_ids)
  AND size(mit_nodes) = size($restore_mitigated_ids) AND size(mit_rm) = size($remove_mitigated_ids)
  AND size(pol) = size(CASE WHEN $restore_policy_id IS NULL THEN [] ELSE [1] END)
  AND (NOT $remove_policy_edge OR size(gov_rm) = 1)
FOREACH (x IN rq_nodes | MERGE (x)-[:REQUIRES]->(a))
FOREACH (r IN rq_rm | DELETE r)
FOREACH (x IN cov_nodes | MERGE (x)-[:COVERS]->(a))
FOREACH (r IN cov_rm | DELETE r)
FOREACH (x IN mit_nodes | MERGE (x)-[:MITIGATED_BY]->(a))
FOREACH (r IN mit_rm | DELETE r)
FOREACH (p IN pol | MERGE (a)-[:GOVERNED_BY]->(p))
FOREACH (r IN CASE WHEN $remove_policy_edge THEN gov_rm ELSE [] END | DELETE r)
SET a.status = 'active'
DELETE m
RETURN a.id AS absorbed_id
"""


def unmerge_capability(
    graph: GraphHandle, *, absorbed_id: str, survivor_id: str, write: CapabilityUnmergeWrite
) -> None:
    """Reverse one capability merge in one guarded, all-or-nothing statement.

    Raises:
        GraphCleanupStaleStateError: the guard did not match (the tombstone or its redirect,
            the survivor's status, an endpoint, an edge count or the Policy differs from the
            plan); nothing was written.
        GraphCleanupPersistenceError: the graph database failed; carries a generic
            message only, the driver error is chained.
    """
    params: dict[str, object] = {
        "absorbed_id": absorbed_id,
        "survivor_id": survivor_id,
        "restore_requires_ids": list(write.restore_requires_ids),
        "remove_requires_ids": list(write.remove_requires_ids),
        "restore_covers_ids": list(write.restore_covers_ids),
        "remove_covers_ids": list(write.remove_covers_ids),
        "restore_mitigated_ids": list(write.restore_mitigated_ids),
        "remove_mitigated_ids": list(write.remove_mitigated_ids),
        "restore_policy_id": write.restore_policy_id,
        "remove_policy_edge": write.remove_policy_edge,
    }
    try:
        rows = query_rows(graph, UNMERGE_CAPABILITY_QUERY, params)
    except GraphCleanupPersistenceError as exc:
        message = "the policy graph database could not complete the write"
        raise GraphCleanupPersistenceError(message) from exc
    if not rows:
        message = "the capabilities changed since the preview; nothing was written"
        raise GraphCleanupStaleStateError(message)


# CHANGES.md A6 (collecting WITHs per the I2 contract): recreates the deleted Obligation under its
# original id and removes only the `MergedObligation` marker. The marker, the absence of the
# Obligation, the Role, every Requirement and every non-tombstone Capability are pinned in the
# guard. Survivor edges are never matched, so none is removed.
UNMERGE_OBLIGATION_QUERY = """
MATCH (m:MergedObligation {id: $absorbed_id}) WHERE m.merged_into = $survivor_id
OPTIONAL MATCH (x:Obligation {id: $absorbed_id})
WITH m, collect(DISTINCT x) AS existing
OPTIONAL MATCH (q:Requirement) WHERE q.id IN $satisfied_by_ids
WITH m, existing, collect(DISTINCT q) AS qs
OPTIONAL MATCH (c:Capability) WHERE c.id IN $requires_ids
WITH m, existing, qs, collect(DISTINCT c) AS cs
MATCH (r:Role {id: $role_id})
WHERE size(existing) = 0 AND size(qs) = size($satisfied_by_ids) AND size(cs) = size($requires_ids)
  AND all(cap IN cs WHERE coalesce(cap.status,'active') <> 'merged')
CREATE (o:Obligation {id: $absorbed_id})
SET o += $properties
MERGE (r)-[:HAS]->(o)
FOREACH (req IN qs | MERGE (req)-[:SATISFIED_BY]->(o))
FOREACH (cap IN cs | MERGE (o)-[:REQUIRES]->(cap))
DELETE m
RETURN o.id AS absorbed_id
"""


def unmerge_obligation(
    graph: GraphHandle, *, absorbed_id: str, survivor_id: str, write: ObligationUnmergeWrite
) -> None:
    """Recreate the deleted Obligation `absorbed_id` in one guarded, all-or-nothing statement.

    Raises:
        GraphCleanupStaleStateError: the guard did not match (the marker is gone or re-pointed,
            the Obligation exists again, the Role, a Requirement or a Capability is gone or a
            Capability is a tombstone); nothing was written.
        GraphCleanupPersistenceError: the graph database failed; carries a generic
            message only, the driver error is chained.
    """
    params: dict[str, object] = {
        "absorbed_id": absorbed_id,
        "survivor_id": survivor_id,
        "role_id": write.role_id,
        "satisfied_by_ids": list(write.satisfied_by_ids),
        "requires_ids": list(write.requires_ids),
        "properties": dict(write.properties),
    }
    try:
        rows = query_rows(graph, UNMERGE_OBLIGATION_QUERY, params)
    except GraphCleanupPersistenceError as exc:
        message = "the policy graph database could not complete the write"
        raise GraphCleanupPersistenceError(message) from exc
    if not rows:
        message = "the obligations changed since the preview; nothing was written"
        raise GraphCleanupStaleStateError(message)
