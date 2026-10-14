"""Frozen copy of Graph Cleanup's five guarded write statements from before the gateway.

The statement texts are the Cypher of the pre-migration `graph_cleanup/graph_writer.py`, verbatim.
Each is pinned by a sha256 taken from `git show HEAD:ps-service/src/ps_service/graph_cleanup/
graph_writer.py` at freeze time (commit 26672f3;
`test_frozen_statements_match_their_pinned_hashes`),
so an edit to a "verbatim" constant fails.

`LegacyRecordingGraph` is a small interpreter that keeps the node and edge sets those statements
would have produced. Only the parameters are read; the statement text selects the shape. A
statement the interpreter does not know raises, so the reference can only claim what it has been
taught. Each migration slice teaches one shape; shapes taught so far: release governance, capability
merge (all four governance cases), obligation merge.

Deviations from the original, each deliberate:

* the `query_rows` connectivity-health wrapper is dropped (it records dependency health, not graph
  state);
* the guard-miss `RETURN` is modelled as the statement returning no rows and changing nothing.

Met modulo edge identity; the live digest and live interpreter agreement are not verifiable here
(no FalkorDB in this sandbox): the `falkordb_live` tests stay as the live anchor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from graph_gateway._fakes import InMemoryGraph

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.graph_cleanup.models import (
        CapabilityUnmergeWrite,
        ExpectedCounts,
        ObligationExpectedCounts,
        ObligationUnmergeWrite,
    )

__all__ = [
    "FROZEN_STATEMENTS",
    "FROZEN_STATEMENT_HASHES",
    "LegacyRecordingGraph",
    "legacy_merge_capabilities",
    "legacy_merge_obligations",
    "legacy_release_governance",
    "legacy_unmerge_capability",
    "legacy_unmerge_obligation",
    "run_frozen_merge_capabilities",
    "run_frozen_merge_obligations",
    "run_frozen_unmerge_capability",
    "run_frozen_unmerge_obligation",
]

EdgeKey = tuple[str, str, str, str, str]
"""`(type, source label, source id, target label, target id)`."""

# sha256 of each statement text, taken from `git show HEAD:ps-service/src/ps_service/graph_cleanup/
# graph_writer.py` (commit 26672f3) at freeze.
FROZEN_STATEMENT_HASHES: dict[str, str] = {
    "merge_capabilities": "791c7140440122cab798e1476894a8c69f8260013f251e3813bdfc868b439a38",
    "merge_obligations": "53330d2a37fbdf397bf3f4f643d44bdf512e801bb40774c07afb1df45752f6ae",
    "release_governance": "6efd371a4e4e7aba7b8a64ac6fcf97c1bb65a51dcb0a8e422bb03acbd622844d",
    "unmerge_capability": "2e67cbee9e0d5922a91239e3df34e117358aac2e58c65431c062eb207f638720",
    "unmerge_obligation": "f0bdbb1a385b8604098810d8462083519edb4a38a2fcb1bc9791bf0ea42c21a3",
}

_MERGE_CAPABILITIES = """
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

_MERGE_OBLIGATIONS = """
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

_RELEASE_GOVERNANCE = """
MATCH (c:Capability {id: $capability_id})
      -[r:GOVERNED_BY]->(p:Policy {id: $policy_id, status: 'draft'})
WHERE coalesce(c.status,'active') = 'active'
DELETE r
RETURN c.id AS capability_id
"""

_UNMERGE_CAPABILITY = """
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

_UNMERGE_OBLIGATION = """
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

FROZEN_STATEMENTS: dict[str, str] = {
    "merge_capabilities": _MERGE_CAPABILITIES,
    "merge_obligations": _MERGE_OBLIGATIONS,
    "release_governance": _RELEASE_GOVERNANCE,
    "unmerge_capability": _UNMERGE_CAPABILITY,
    "unmerge_obligation": _UNMERGE_OBLIGATION,
}


@dataclass
class _Result:
    result_set: list[list[object]] = field(default_factory=lambda: cast("list[list[object]]", []))


@dataclass
class LegacyRecordingGraph:
    """A legacy `GraphHandle`: records every write statement and the graph those calls build."""

    nodes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    edges: dict[EdgeKey, dict[str, object]] = field(default_factory=dict)
    statements: list[str] = field(default_factory=list)

    @classmethod
    def copy_of(cls, graph: InMemoryGraph) -> LegacyRecordingGraph:
        """The nodes and edges of `graph` as a legacy graph (an edge's identity is not kept)."""
        copy = cls()
        copy.nodes = {key: dict(props) for key, props in graph.nodes.items()}
        copy.edges = {
            (key[0], key[1], key[2], key[3], key[4]): {
                name: value for name, value in props.items() if name != "identity"
            }
            for key, props in graph.edges.items()
        }
        return copy

    def as_gateway_graph(self) -> InMemoryGraph:
        """The graph the calls built, every edge stamped with the gateway's identity.

        The comparison is "modulo edge identity": the legacy graph carries no identity, the
        gateway stamps `TYPE:source->target` on every edge it writes.
        """
        graph = InMemoryGraph()
        for (label, node_id), properties in self.nodes.items():
            graph.seed_node((label,), node_id, properties)
        for (
            edge_type,
            source_label,
            source_id,
            target_label,
            target_id,
        ), props in self.edges.items():
            identity = f"{edge_type}:{source_id}->{target_id}"
            key = (edge_type, source_label, source_id, target_label, target_id, identity)
            graph.edges[key] = {"identity": identity, **props}
            graph.edge_internal_id(key)
        return graph

    # -- the interpreter -----------------------------------------------------------------------

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Apply one frozen legacy statement; any other shape is a failure of the reference."""
        assert params is not None, f"a frozen write statement carries parameters: {q}"
        self.statements.append(q)
        handler = self._dispatch(q)
        if handler is None:
            raise AssertionError(f"not a frozen legacy statement the reference was taught: {q}")
        return handler(params)

    def _dispatch(self, q: str) -> Callable[[dict[str, object]], _Result] | None:
        fixed: dict[str, Callable[[dict[str, object]], _Result]] = {
            _RELEASE_GOVERNANCE: self._release_governance,
            _MERGE_CAPABILITIES: self._merge_capabilities,
            _MERGE_OBLIGATIONS: self._merge_obligations,
            _UNMERGE_CAPABILITY: self._unmerge_capability,
            _UNMERGE_OBLIGATION: self._unmerge_obligation,
        }
        return fixed.get(q)

    def _release_governance(self, params: dict[str, object]) -> _Result:
        """`MATCH (c)-[r:GOVERNED_BY]->(p {status:'draft'}) WHERE c active DELETE r RETURN`."""
        capability_id = cast("str", params["capability_id"])
        policy_id = cast("str", params["policy_id"])
        capability = self.nodes.get(("Capability", capability_id))
        policy = self.nodes.get(("Policy", policy_id))
        key: EdgeKey = ("GOVERNED_BY", "Capability", capability_id, "Policy", policy_id)
        if (
            capability is None
            or policy is None
            or capability.get("status", "active") != "active"
            or policy.get("status") != "draft"
            or key not in self.edges
        ):
            return _Result()
        del self.edges[key]
        return _Result([[capability_id]])

    # -- merge-capabilities --------------------------------------------------------------------

    def _edges_into(
        self, edge_type: str, source_label: str, target: tuple[str, str]
    ) -> list[EdgeKey]:
        """Keys of the `edge_type` edges from a `source_label` node into `target`."""
        return [
            key
            for key in self.edges
            if key[0] == edge_type and key[1] == source_label and (key[3], key[4]) == target
        ]

    def _governing_policies(self, capability_id: str) -> tuple[list[str], list[EdgeKey]]:
        """The distinct existing Policies a Capability is governed by, and those edges."""
        keys = [
            key
            for key in self.edges
            if key[:3] == ("GOVERNED_BY", "Capability", capability_id)
            and key[3] == "Policy"
            and ("Policy", key[4]) in self.nodes
        ]
        return list(dict.fromkeys(key[4] for key in keys)), keys

    def _merge_capabilities(self, params: dict[str, object]) -> _Result:
        """The capability merge: guard, then move edges, tombstone, `MERGED_INTO` (one row)."""
        survivor_id = cast("str", params["survivor_id"])
        absorbed_id = cast("str", params["absorbed_id"])
        survivor = self.nodes.get(("Capability", survivor_id))
        absorbed = self.nodes.get(("Capability", absorbed_id))
        if (
            survivor is None
            or absorbed is None
            or survivor.get("status", "active") != "active"
            or absorbed.get("status", "active") != "active"
        ):
            return _Result()
        incoming = {
            edge_type: self._edges_into(edge_type, source_label, ("Capability", absorbed_id))
            for edge_type, source_label in _CAPABILITY_EDGE_CLASSES
        }
        governing_absorbed, governing_absorbed_edges = self._governing_policies(absorbed_id)
        governing_survivor, _ = self._governing_policies(survivor_id)
        if (
            len(incoming["REQUIRES"]) != params["expected_requires"]
            or len(incoming["COVERS"]) != params["expected_covers"]
            or len(incoming["MITIGATED_BY"]) != params["expected_mitigated"]
            or len(governing_absorbed) != params["expected_absorbed_governed"]
            or len(governing_survivor) != params["expected_survivor_governed"]
            or not all(
                policy == params["expected_absorbed_policy_id"]
                and self.nodes[("Policy", policy)].get("status")
                == params["expected_absorbed_policy_status"]
                for policy in governing_absorbed
            )
            or not all(
                policy == params["expected_survivor_policy_id"]
                and self.nodes[("Policy", policy)].get("status")
                == params["expected_survivor_policy_status"]
                for policy in governing_survivor
            )
            or (
                len(governing_absorbed) == 1
                and len(governing_survivor) == 1
                and governing_absorbed[0] != governing_survivor[0]
            )
        ):
            return _Result()
        for edge_type, keys in incoming.items():
            for key in keys:
                self.edges.setdefault((edge_type, key[1], key[2], "Capability", survivor_id), {})
        if not governing_survivor:
            for policy in governing_absorbed:
                self.edges.setdefault(
                    ("GOVERNED_BY", "Capability", survivor_id, "Policy", policy), {}
                )
        for key in [k for keys in incoming.values() for k in keys] + governing_absorbed_edges:
            del self.edges[key]
        absorbed["status"] = "merged"
        self.edges.setdefault(
            ("MERGED_INTO", "Capability", absorbed_id, "Capability", survivor_id), {}
        )
        return _Result([[survivor_id, absorbed_id]])

    # -- merge-obligations ---------------------------------------------------------------------

    def _merge_obligations(self, params: dict[str, object]) -> _Result:
        """The obligation merge: guard, union the edges, leave a marker, detach-delete."""
        survivor_id = cast("str", params["survivor_id"])
        absorbed_id = cast("str", params["absorbed_id"])
        if survivor_id == absorbed_id:
            return _Result()
        has = {(key[2], key[4]) for key in self.edges if key[0] == "HAS" and key[1] == "Role"}
        shared_role = any(
            (role, survivor_id) in has and (role, absorbed_id) in has for role, _ in has
        )
        if (
            not shared_role
            or ("Obligation", survivor_id) not in self.nodes
            or ("Obligation", absorbed_id) not in self.nodes
        ):
            return _Result()
        satisfied = self._edges_into("SATISFIED_BY", "Requirement", ("Obligation", absorbed_id))
        requires = [
            key
            for key in self.edges
            if key[:3] == ("REQUIRES", "Obligation", absorbed_id) and key[3] == "Capability"
        ]
        if (
            len(satisfied) != params["expected_satisfied"]
            or len(requires) != params["expected_requires"]
        ):
            return _Result()
        for key in satisfied:
            self.edges.setdefault(
                ("SATISFIED_BY", "Requirement", key[2], "Obligation", survivor_id), {}
            )
        for key in requires:
            self.edges.setdefault(("REQUIRES", "Obligation", survivor_id, "Capability", key[4]), {})
        self.nodes.setdefault(("MergedObligation", absorbed_id), {})["merged_into"] = survivor_id
        for key in [
            k for k in self.edges if ("Obligation", absorbed_id) in {(k[1], k[2]), (k[3], k[4])}
        ]:
            del self.edges[key]
        del self.nodes[("Obligation", absorbed_id)]
        return _Result([[survivor_id, absorbed_id]])

    # -- unmerge-capability --------------------------------------------------------------------

    def _unmerge_capability(self, params: dict[str, object]) -> _Result:
        """The capability unmerge: guard, then restore the moved edges and un-tombstone."""
        absorbed_id = cast("str", params["absorbed_id"])
        survivor_id = cast("str", params["survivor_id"])
        absorbed = self.nodes.get(("Capability", absorbed_id))
        survivor = self.nodes.get(("Capability", survivor_id))
        redirect: EdgeKey = ("MERGED_INTO", "Capability", absorbed_id, "Capability", survivor_id)
        if (
            absorbed is None
            or survivor is None
            or absorbed.get("status") != "merged"
            or redirect not in self.edges
            or survivor.get("status", "active") != "active"
        ):
            return _Result()
        policy_id = cast("str | None", params["restore_policy_id"])
        classes = (
            ("REQUIRES", "Obligation", "requires"),
            ("COVERS", "PracticeArea", "covers"),
            ("MITIGATED_BY", "RiskPath", "mitigated"),
        )
        restore: list[EdgeKey] = []
        remove: list[EdgeKey] = []
        for edge_type, label, name in classes:
            restore_ids = cast("list[str]", params[f"restore_{name}_ids"])
            remove_ids = cast("list[str]", params[f"remove_{name}_ids"])
            if sum(1 for i in restore_ids if (label, i) in self.nodes) != len(restore_ids):
                return _Result()
            wanted = [(edge_type, label, i, "Capability", survivor_id) for i in remove_ids]
            if sum(1 for key in wanted if key in self.edges) != len(remove_ids):
                return _Result()
            restore.extend((edge_type, label, i, "Capability", absorbed_id) for i in restore_ids)
            remove.extend(wanted)
        governed: EdgeKey | None = None
        if policy_id is not None:
            if ("Policy", policy_id) not in self.nodes:
                return _Result()
            governed = ("GOVERNED_BY", "Capability", survivor_id, "Policy", policy_id)
        if params["remove_policy_edge"] and (governed is None or governed not in self.edges):
            return _Result()
        for key in restore:
            self.edges.setdefault(key, {})
        for key in remove:
            del self.edges[key]
        if policy_id is not None:
            self.edges.setdefault(
                ("GOVERNED_BY", "Capability", absorbed_id, "Policy", policy_id), {}
            )
        if params["remove_policy_edge"] and governed is not None:
            del self.edges[governed]
        absorbed["status"] = "active"
        del self.edges[redirect]
        return _Result([[absorbed_id]])

    # -- unmerge-obligation --------------------------------------------------------------------

    def _unmerge_obligation(self, params: dict[str, object]) -> _Result:
        """The obligation unmerge: guard, then recreate the Obligation and drop the marker."""
        absorbed_id = cast("str", params["absorbed_id"])
        survivor_id = cast("str", params["survivor_id"])
        role_id = cast("str", params["role_id"])
        satisfied_by = cast("list[str]", params["satisfied_by_ids"])
        requires = cast("list[str]", params["requires_ids"])
        marker = self.nodes.get(("MergedObligation", absorbed_id))
        if (
            marker is None
            or marker.get("merged_into") != survivor_id
            or ("Obligation", absorbed_id) in self.nodes
            or ("Role", role_id) not in self.nodes
            or any(("Requirement", i) not in self.nodes for i in satisfied_by)
            or any(("Capability", i) not in self.nodes for i in requires)
            or any(
                self.nodes[("Capability", i)].get("status", "active") == "merged" for i in requires
            )
        ):
            return _Result()
        self.nodes[("Obligation", absorbed_id)] = dict(
            cast("dict[str, object]", params["properties"])
        )
        self.edges.setdefault(("HAS", "Role", role_id, "Obligation", absorbed_id), {})
        for requirement in satisfied_by:
            self.edges.setdefault(
                ("SATISFIED_BY", "Requirement", requirement, "Obligation", absorbed_id), {}
            )
        for capability in requires:
            self.edges.setdefault(
                ("REQUIRES", "Obligation", absorbed_id, "Capability", capability), {}
            )
        del self.nodes[("MergedObligation", absorbed_id)]
        return _Result([[absorbed_id]])


_CAPABILITY_EDGE_CLASSES: tuple[tuple[str, str], ...] = (
    ("REQUIRES", "Obligation"),
    ("COVERS", "PracticeArea"),
    ("MITIGATED_BY", "RiskPath"),
)


def legacy_merge_capabilities(
    graph: LegacyRecordingGraph,
    *,
    survivor_id: str,
    absorbed_id: str,
    expected: ExpectedCounts,
) -> bool:
    """Run the frozen capability-merge statement; `True` when it matched, `False` on a miss."""
    return run_frozen_merge_capabilities(
        cast("GraphHandle", graph),
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        expected=expected,
    )


def legacy_merge_obligations(
    graph: LegacyRecordingGraph,
    *,
    survivor_id: str,
    absorbed_id: str,
    expected: ObligationExpectedCounts,
) -> bool:
    """Run the frozen obligation-merge statement; `True` when it matched, `False` on a miss."""
    return run_frozen_merge_obligations(
        cast("GraphHandle", graph),
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        expected=expected,
    )


def _capability_params(
    survivor_id: str, absorbed_id: str, expected: ExpectedCounts
) -> dict[str, object]:
    return {
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


def _obligation_params(
    survivor_id: str, absorbed_id: str, expected: ObligationExpectedCounts
) -> dict[str, object]:
    return {
        "survivor_id": survivor_id,
        "absorbed_id": absorbed_id,
        "expected_satisfied": expected.satisfied,
        "expected_requires": expected.requires,
    }


def run_frozen_merge_capabilities(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str, expected: ExpectedCounts
) -> bool:
    """Run the frozen capability-merge statement raw on a real graph (`falkordb_live` anchor)."""
    params = _capability_params(survivor_id, absorbed_id, expected)
    return bool(graph.query(_MERGE_CAPABILITIES, params).result_set)


def run_frozen_merge_obligations(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str, expected: ObligationExpectedCounts
) -> bool:
    """Run the frozen obligation-merge statement raw on a real graph (`falkordb_live` anchor)."""
    params = _obligation_params(survivor_id, absorbed_id, expected)
    return bool(graph.query(_MERGE_OBLIGATIONS, params).result_set)


def legacy_release_governance(
    graph: LegacyRecordingGraph, *, capability_id: str, policy_id: str
) -> bool:
    """Run the frozen release statement; `True` when it matched, `False` on a guard miss."""
    params: dict[str, object] = {"capability_id": capability_id, "policy_id": policy_id}
    return bool(graph.query(_RELEASE_GOVERNANCE, params).result_set)


def _unmerge_capability_params(
    survivor_id: str, absorbed_id: str, write: CapabilityUnmergeWrite
) -> dict[str, object]:
    return {
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


def _unmerge_obligation_params(
    survivor_id: str, absorbed_id: str, write: ObligationUnmergeWrite
) -> dict[str, object]:
    return {
        "absorbed_id": absorbed_id,
        "survivor_id": survivor_id,
        "role_id": write.role_id,
        "satisfied_by_ids": list(write.satisfied_by_ids),
        "requires_ids": list(write.requires_ids),
        "properties": dict(write.properties),
    }


def run_frozen_unmerge_capability(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str, write: CapabilityUnmergeWrite
) -> bool:
    """Run the frozen capability-unmerge statement raw on a real graph (`falkordb_live` anchor)."""
    params = _unmerge_capability_params(survivor_id, absorbed_id, write)
    return bool(graph.query(_UNMERGE_CAPABILITY, params).result_set)


def run_frozen_unmerge_obligation(
    graph: GraphHandle, *, survivor_id: str, absorbed_id: str, write: ObligationUnmergeWrite
) -> bool:
    """Run the frozen obligation-unmerge statement raw on a real graph (`falkordb_live` anchor)."""
    params = _unmerge_obligation_params(survivor_id, absorbed_id, write)
    return bool(graph.query(_UNMERGE_OBLIGATION, params).result_set)


def legacy_unmerge_capability(
    graph: LegacyRecordingGraph,
    *,
    survivor_id: str,
    absorbed_id: str,
    write: CapabilityUnmergeWrite,
) -> bool:
    """Run the frozen capability-unmerge statement; `True` when it matched, `False` on a miss."""
    return run_frozen_unmerge_capability(
        cast("GraphHandle", graph), survivor_id=survivor_id, absorbed_id=absorbed_id, write=write
    )


def legacy_unmerge_obligation(
    graph: LegacyRecordingGraph,
    *,
    survivor_id: str,
    absorbed_id: str,
    write: ObligationUnmergeWrite,
) -> bool:
    """Run the frozen obligation-unmerge statement; `True` when it matched, `False` on a miss."""
    return run_frozen_unmerge_obligation(
        cast("GraphHandle", graph), survivor_id=survivor_id, absorbed_id=absorbed_id, write=write
    )
