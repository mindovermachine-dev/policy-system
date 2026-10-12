"""Frozen copy of Policy Lifecycle's graph writer as it was before issue #211: the reference.

It issues the Cypher the pre-migration `policy_lifecycle/graph_writer.py` issued, in the same
order, against a `LegacyRecordingGraph`: a small interpreter that keeps the node and edge sets those
statements would have produced. Only the parameters are read; the statement text selects the shape.
A statement the interpreter does not know raises, so the reference can only claim what it has been
taught.

Frozen shapes: the three status/version backfills, the status cascade, the guarded fork repoint,
the guarded capability-claim create and the plain create (Policy, `SUPERSEDED_BY`, Standard and
Control children), the three null-clearing PATCH writers and the two child adds. Each statement text
is pinned by a sha256 taken from `git show HEAD:ps-service/src/ps_service/policy_lifecycle/
graph_writer.py` at freeze time (`test_frozen_statements_match_their_pinned_hashes`), so an edit to
a "verbatim" constant fails.

Deviations from the original, each deliberate:

* the `_execute_query` connectivity-health wrapper is dropped (it records dependency health, not
  graph state);
* `MERGE (p)-[:SUPPORTED_BY]->(s:Standard {id})` over a Standard that already exists under ANOTHER
  parent (or has no parent) creates a second node with the same id in FalkorDB (the `MERGE` is on
  the whole pattern; characterised live on 2026-10-08, unreachable through the computed ids). This
  interpreter keys nodes by `(label, id)` and cannot hold two, so it RAISES on that input instead
  of reproducing it; the gateway's upsert-by-id deliberately does not reproduce it either (the one
  intentional divergence of #211);
* a `SET x += $map` whose map carries a `None` value, or an `id` different from the merge key, is
  refused (the original never passed one on a reachable path and FalkorDB's treatment of either is
  not characterised).

Met modulo edge identity; the live digest and live interpreter agreement are not verifiable here
(no FalkorDB in this sandbox): the `falkordb_live` tests stay as the live anchor.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from graph_gateway._fakes import InMemoryGraph

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ps_service.policy_lifecycle.graph_writer import StandardDraft

__all__ = [
    "FROZEN_STATEMENTS",
    "FROZEN_STATEMENT_HASHES",
    "LegacyRecordingGraph",
    "LegacyWriteTarget",
    "legacy_add_control_to_standard",
    "legacy_add_standard_to_policy",
    "legacy_approve_fork_repoint",
    "legacy_backfill_governance_status",
    "legacy_cascade_status",
    "legacy_create_policy_draft",
    "legacy_update_control_fields",
    "legacy_update_policy_fields",
    "legacy_update_standard_fields",
]

EdgeKey = tuple[str, str, str, str, str]
"""`(type, source label, source id, target label, target id)`."""

# sha256 of each rendered statement text, taken from `git show HEAD:ps-service/src/ps_service/
# policy_lifecycle/graph_writer.py` (commit dcbee56) at freeze. The null-clearing statements are
# pinned as rendered for the key `k`.
FROZEN_STATEMENT_HASHES: dict[str, str] = {
    "backfill_version": "8c6d92a3507445b6b3211d385315435dcd9230a1181522015dde06f00ec640a3",
    "backfill_standard_status": "e532c117fef3cea8c5926437fe8e206591dd4d87cd261d0e831b9dfc076c0b10",
    "backfill_control_status": "f1f9346408c651c5e029f6d5e8597d360d8470b6cf54d813caa137d4db70d7fe",
    "cascade": "90312e25ac59944c0ac85634727e92b8608e2ebe793c4e362c221701a6aac195",
    "repoint": "9cfdacf73daee0c2a0fb5951c6feff5c41719264249048a602986e3aa2906e1d",
    "claim_create": "0201c25371a57c7f2efb5ba9af2d5d5944ceffa8036604ea1ce255d816777cf1",
    "plain_create": "640c53412ff029893e62365f3d4877d7960383ec166f4917868582feac0d2fe7",
    "supersede": "0e134ab75b3b1a2fbd9d1fe4fa3ff86f66f53404b27e88a1dc76c0dca9262bb5",
    "standard_child": "f149d3e83dc87cd7dae91c84c6565f46436ecf129ee235b38d15743df6e3962e",
    "control_child": "46c9d318b859666ece327fe49f652ed43c6dd7821e86efd4626c2da2c3ced13e",
    "update_policy_set": "bb79e4ad4bef79a29815f23509f7b7ac2e65cfa22e0d75712bf17cc948c0af0d",
    "update_standard_set": "a54eaaaae4081e4ccb81eacc868c8068a89ca847ff3df58b2bec431afe641dbd",
    "update_control_set": "67e67254bd18810d0b66bc31bd9dff340ff78fa059a24c877963f1ca3adcf842",
    "clear_policy_key": "11d67b5a9689ef1f25132789bb1464bb50b0a6cd1e6e2a57f85c03925d30e90e",
    "clear_standard_key": "56e71e0e31408747b3582de5cd62ee7d764b29c69085aacb4bd7ce7957491ff6",
    "clear_control_key": "30bafad07dd945c8151af4bdf6f6ca84f4a937d4a6d59ece43bc0ccf83b15f83",
}

_BACKFILL_VERSION = "MATCH (p:Policy {id: $policy_id}) SET p.version = coalesce(p.version, '1')"
_BACKFILL_STANDARD = (
    "MATCH (p:Policy {id: $policy_id})-[:SUPPORTED_BY]->(s:Standard) "
    "WHERE s.status IS NULL "
    "SET s.status = p.status"
)
_BACKFILL_CONTROL = (
    "MATCH (p:Policy {id: $policy_id})-[:SUPPORTED_BY]->(:Standard)"
    "-[:IMPLEMENTED_BY]->(c:Control) "
    "WHERE c.status IS NULL "
    "SET c.status = p.status"
)
_CASCADE = (
    "MATCH (p:Policy {id: $policy_id}) "
    "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
    "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c:Control) "
    "SET p.status = $target_status, s.status = $target_status, "
    "c.status = $target_status"
)
_REPOINT = (
    "MATCH (p:Policy {id: $policy_id}) "
    "OPTIONAL MATCH (cap:Capability)-[r:GOVERNED_BY]->(:Policy {id: $prior_id}) "
    "WHERE cap.id IN $capability_ids "
    "WITH p, collect(cap) AS caps, collect(r) AS rels WHERE size(rels) = $expected "
    "FOREACH (r IN rels | DELETE r) "
    "FOREACH (c IN caps | MERGE (c)-[:GOVERNED_BY]->(p)) "
    "WITH p "
    "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(s:Standard) "
    "OPTIONAL MATCH (s)-[:IMPLEMENTED_BY]->(c2:Control) "
    "SET p.status = $target_status, s.status = $target_status, "
    "c2.status = $target_status "
    "RETURN p.id"
)
_CLAIM_CREATE = (
    "MATCH (cap:Capability) WHERE cap.id IN $capability_ids "
    "AND coalesce(cap.status,'active') <> 'merged' "
    "AND NOT (cap)-[:GOVERNED_BY]->(:Policy) "
    "WITH collect(cap) AS caps WHERE size(caps) = $expected "
    "MERGE (p:Policy {id: $policy_id}) SET p += $properties "
    "FOREACH (c IN caps | MERGE (c)-[:GOVERNED_BY]->(p)) "
    "RETURN p.id"
)
_PLAIN_CREATE = "MERGE (p:Policy {id: $policy_id}) SET p += $properties"
_SUPERSEDE = (
    "MATCH (prior:Policy {id: $prior_id}), (new:Policy {id: $new_id}) "
    "MERGE (prior)-[:SUPERSEDED_BY]->(new)"
)
_STANDARD_CHILD = (
    "MATCH (p:Policy {id: $policy_id}) "
    "MERGE (p)-[:SUPPORTED_BY]->(s:Standard {id: $standard_id}) "
    "SET s += $properties"
)
_CONTROL_CHILD = (
    "MATCH (s:Standard {id: $standard_id}) "
    "MERGE (s)-[:IMPLEMENTED_BY]->(c:Control {id: $control_id}) "
    "SET c += $properties"
)
_UPDATE_POLICY_SET = "MATCH (p:Policy {id: $policy_id}) SET p += $set_properties"
_UPDATE_STANDARD_SET = "MATCH (s:Standard {id: $standard_id}) SET s += $set_properties"
_UPDATE_CONTROL_SET = "MATCH (c:Control {id: $control_id}) SET c += $set_properties"


def _clear_policy(key: str) -> str:
    return f"MATCH (p:Policy {{id: $policy_id}}) SET p.{key} = null"


def _clear_standard(key: str) -> str:
    return f"MATCH (s:Standard {{id: $standard_id}}) SET s.{key} = null"


def _clear_control(key: str) -> str:
    return f"MATCH (c:Control {{id: $control_id}}) SET c.{key} = null"


FROZEN_STATEMENTS: dict[str, str] = {
    "backfill_version": _BACKFILL_VERSION,
    "backfill_standard_status": _BACKFILL_STANDARD,
    "backfill_control_status": _BACKFILL_CONTROL,
    "cascade": _CASCADE,
    "repoint": _REPOINT,
    "claim_create": _CLAIM_CREATE,
    "plain_create": _PLAIN_CREATE,
    "supersede": _SUPERSEDE,
    "standard_child": _STANDARD_CHILD,
    "control_child": _CONTROL_CHILD,
    "update_policy_set": _UPDATE_POLICY_SET,
    "update_standard_set": _UPDATE_STANDARD_SET,
    "update_control_set": _UPDATE_CONTROL_SET,
    "clear_policy_key": _clear_policy("k"),
    "clear_standard_key": _clear_standard("k"),
    "clear_control_key": _clear_control("k"),
}

_CLEAR_POLICY = re.compile(r"^MATCH \(p:Policy \{id: \$policy_id\}\) SET p\.(\w+) = null$")
_CLEAR_STANDARD = re.compile(r"^MATCH \(s:Standard \{id: \$standard_id\}\) SET s\.(\w+) = null$")
_CLEAR_CONTROL = re.compile(r"^MATCH \(c:Control \{id: \$control_id\}\) SET c\.(\w+) = null$")


class LegacyWriteTarget(Protocol):
    """Where the frozen statements run: the recording graph here, a real FalkorDB graph live."""

    def query(self, q: str, params: dict[str, object] | None = None) -> object:
        """Run one legacy statement."""
        ...


@dataclass
class _Result:
    result_set: list[object] = field(default_factory=list)


@dataclass
class LegacyRecordingGraph:
    """A legacy `GraphHandle`: records every write statement and the graph those calls build."""

    nodes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    edges: dict[EdgeKey, dict[str, object]] = field(default_factory=dict)
    parallel_edges: Counter[EdgeKey] = field(default_factory=Counter)
    """Extra copies of an edge beyond the first (a graph can hold parallel `GOVERNED_BY` edges)."""
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

    def seed_into(self, graph: InMemoryGraph) -> None:
        """Put this graph's nodes and edges into `graph` as a pre-gateway writer left them."""
        for (label, node_id), props in self.nodes.items():
            graph.seed_node((label,), node_id, props)
        for key, props in self.edges.items():
            graph.seed_legacy_edge(key, props)

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

    def seed_parallel_edge(self, key: EdgeKey) -> None:
        """Add a second, parallel copy of an existing edge (FalkorDB allows several)."""
        assert key in self.edges, f"no edge to duplicate: {key}"
        self.parallel_edges[key] += 1

    # -- the interpreter -----------------------------------------------------------------------

    def query(self, q: str, params: dict[str, object] | None = None) -> _Result:
        """Apply one frozen legacy statement; any other shape is a failure of the reference."""
        assert params is not None, f"a frozen write statement carries parameters: {q}"
        self.statements.append(q)
        handler = self._dispatch(q)
        if handler is None:
            raise AssertionError(f"not a frozen legacy statement: {q}")
        return handler(params)

    def _dispatch(self, q: str) -> Callable[[dict[str, object]], _Result] | None:
        fixed: dict[str, Callable[[dict[str, object]], _Result]] = {
            _BACKFILL_VERSION: self._backfill_version,
            _BACKFILL_STANDARD: self._backfill_standard,
            _BACKFILL_CONTROL: self._backfill_control,
            _CASCADE: self._cascade,
            _REPOINT: self._repoint,
            _CLAIM_CREATE: self._claim_create,
            _PLAIN_CREATE: self._plain_create,
            _SUPERSEDE: self._supersede,
            _STANDARD_CHILD: self._standard_child,
            _CONTROL_CHILD: self._control_child,
            _UPDATE_POLICY_SET: lambda p: self._patch("Policy", cast("str", p["policy_id"]), p),
            _UPDATE_STANDARD_SET: lambda p: self._patch(
                "Standard", cast("str", p["standard_id"]), p
            ),
            _UPDATE_CONTROL_SET: lambda p: self._patch("Control", cast("str", p["control_id"]), p),
        }
        if q in fixed:
            return fixed[q]
        for pattern, label, id_param in (
            (_CLEAR_POLICY, "Policy", "policy_id"),
            (_CLEAR_STANDARD, "Standard", "standard_id"),
            (_CLEAR_CONTROL, "Control", "control_id"),
        ):
            match = pattern.match(q)
            if match is not None:
                key = match.group(1)
                return lambda p, label=label, id_param=id_param, key=key: self._clear(
                    label, cast("str", p[id_param]), key
                )
        return None

    # -- helpers -------------------------------------------------------------------------------

    def _targets(self, edge_type: str, source: tuple[str, str], target_label: str) -> list[str]:
        """Ids of `target_label` nodes `source` points at by `edge_type`, in insertion order."""
        return [
            key[4]
            for key in self.edges
            if key[0] == edge_type and (key[1], key[2]) == source and key[3] == target_label
        ]

    def _tree_rows(self, policy_id: str) -> list[tuple[str | None, str | None]]:
        """The `OPTIONAL MATCH` rows of a Policy's tree: `(standard id|None, control id|None)`."""
        rows: list[tuple[str | None, str | None]] = []
        standards = self._targets("SUPPORTED_BY", ("Policy", policy_id), "Standard")
        if not standards:
            return [(None, None)]
        for standard_id in standards:
            controls = self._targets("IMPLEMENTED_BY", ("Standard", standard_id), "Control")
            rows.extend((standard_id, control_id) for control_id in controls)
            if not controls:
                rows.append((standard_id, None))
        return rows

    @staticmethod
    def _set(node: dict[str, object], key: str, value: object) -> None:
        """`SET n.key = value`: a null value removes the property."""
        if value is None:
            node.pop(key, None)
        else:
            node[key] = value

    def _merge_node(self, label: str, node_id: str, properties: Mapping[str, object]) -> None:
        """`MERGE (n:Label {id}) SET n += properties` for a node matched by id alone."""
        self._check_map(node_id, properties)
        node = self.nodes.setdefault((label, node_id), {})
        node.update({k: v for k, v in properties.items() if k != "id"})

    @staticmethod
    def _check_map(node_id: str, properties: Mapping[str, object]) -> None:
        for key, value in properties.items():
            assert value is not None, f"`+=` with a null value is not characterised: {key}"
            assert key != "id" or value == node_id, "`+=` rewriting the merge key is refused"

    def _merge_edge(self, edge_type: str, source: tuple[str, str], target: tuple[str, str]) -> None:
        self.edges.setdefault((edge_type, *source, *target), {})

    # -- statements ----------------------------------------------------------------------------

    def _backfill_version(self, params: dict[str, object]) -> _Result:
        node = self.nodes.get(("Policy", cast("str", params["policy_id"])))
        if node is not None and node.get("version") is None:
            node["version"] = "1"
        return _Result()

    def _backfill_standard(self, params: dict[str, object]) -> _Result:
        policy_id = cast("str", params["policy_id"])
        policy = self.nodes.get(("Policy", policy_id))
        if policy is None:
            return _Result()
        for standard_id in self._targets("SUPPORTED_BY", ("Policy", policy_id), "Standard"):
            standard = self.nodes[("Standard", standard_id)]
            if standard.get("status") is None:
                self._set(standard, "status", policy.get("status"))
        return _Result()

    def _backfill_control(self, params: dict[str, object]) -> _Result:
        policy_id = cast("str", params["policy_id"])
        policy = self.nodes.get(("Policy", policy_id))
        if policy is None:
            return _Result()
        for standard_id in self._targets("SUPPORTED_BY", ("Policy", policy_id), "Standard"):
            for control_id in self._targets("IMPLEMENTED_BY", ("Standard", standard_id), "Control"):
                control = self.nodes[("Control", control_id)]
                if control.get("status") is None:
                    self._set(control, "status", policy.get("status"))
        return _Result()

    def _cascade_tree(self, policy_id: str, target_status: object) -> None:
        self._set(self.nodes[("Policy", policy_id)], "status", target_status)
        for standard_id, control_id in self._tree_rows(policy_id):
            if standard_id is not None:
                self._set(self.nodes[("Standard", standard_id)], "status", target_status)
            if control_id is not None:
                self._set(self.nodes[("Control", control_id)], "status", target_status)

    def _cascade(self, params: dict[str, object]) -> _Result:
        policy_id = cast("str", params["policy_id"])
        if ("Policy", policy_id) in self.nodes:
            self._cascade_tree(policy_id, params["target_status"])
        return _Result()

    def _repoint(self, params: dict[str, object]) -> _Result:
        policy_id, prior_id = cast("str", params["policy_id"]), cast("str", params["prior_id"])
        if ("Policy", policy_id) not in self.nodes:
            return _Result()  # the leading MATCH finds nothing: no row
        wanted = set(cast("list[str]", params["capability_ids"]))
        governed = [
            key
            for key in self.edges
            if key[0] == "GOVERNED_BY"
            and key[1] == "Capability"
            and (key[3], key[4]) == ("Policy", prior_id)
            and key[2] in wanted
        ]
        seen = sum(1 + self.parallel_edges[key] for key in governed)
        if seen != params["expected"]:
            return _Result()  # the WHERE size(rels) = $expected guard: no row, nothing written
        for key in governed:
            del self.edges[key]
            self.parallel_edges.pop(key, None)
            self._merge_edge("GOVERNED_BY", ("Capability", key[2]), ("Policy", policy_id))
        self._cascade_tree(policy_id, params["target_status"])
        return _Result([[policy_id]])

    def _claim_create(self, params: dict[str, object]) -> _Result:
        wanted = set(cast("list[str]", params["capability_ids"]))
        claimable = [
            node_id
            for (label, node_id), props in self.nodes.items()
            if label == "Capability"
            and node_id in wanted
            and props.get("status", "active") != "merged"
            and not self._targets("GOVERNED_BY", ("Capability", node_id), "Policy")
        ]
        if len(claimable) != params["expected"]:
            return _Result()  # the guard: no row, nothing written
        policy_id = cast("str", params["policy_id"])
        self._merge_node("Policy", policy_id, cast("dict[str, object]", params["properties"]))
        for capability_id in claimable:
            self._merge_edge("GOVERNED_BY", ("Capability", capability_id), ("Policy", policy_id))
        return _Result([[policy_id]])

    def _plain_create(self, params: dict[str, object]) -> _Result:
        self._merge_node(
            "Policy",
            cast("str", params["policy_id"]),
            cast("dict[str, object]", params["properties"]),
        )
        return _Result()

    def _supersede(self, params: dict[str, object]) -> _Result:
        prior = ("Policy", cast("str", params["prior_id"]))
        new = ("Policy", cast("str", params["new_id"]))
        if prior in self.nodes and new in self.nodes:  # MATCH finds an endpoint or nothing
            self._merge_edge("SUPERSEDED_BY", prior, new)
        return _Result()

    def _merge_child(
        self,
        parent: tuple[str, str],
        edge_type: str,
        child: tuple[str, str],
        properties: Mapping[str, object],
    ) -> None:
        """`MATCH (parent) MERGE (parent)-[:T]->(child {id}) SET child += properties`."""
        if parent not in self.nodes:
            return  # the MATCH finds nothing
        edge = (edge_type, *parent, *child)
        if child in self.nodes and edge not in self.edges:
            raise AssertionError(
                f"MERGE would create a second {child[0]} node with id {child[1]}: not modelled"
            )
        self._check_map(child[1], properties)
        self.edges.setdefault(edge, {})
        self.nodes.setdefault(child, {}).update({k: v for k, v in properties.items() if k != "id"})

    def _standard_child(self, params: dict[str, object]) -> _Result:
        self._merge_child(
            ("Policy", cast("str", params["policy_id"])),
            "SUPPORTED_BY",
            ("Standard", cast("str", params["standard_id"])),
            cast("dict[str, object]", params["properties"]),
        )
        return _Result()

    def _control_child(self, params: dict[str, object]) -> _Result:
        self._merge_child(
            ("Standard", cast("str", params["standard_id"])),
            "IMPLEMENTED_BY",
            ("Control", cast("str", params["control_id"])),
            cast("dict[str, object]", params["properties"]),
        )
        return _Result()

    def _patch(self, label: str, node_id: str, params: dict[str, object]) -> _Result:
        node = self.nodes.get((label, node_id))
        if node is not None:
            properties = cast("dict[str, object]", params["set_properties"])
            self._check_map(node_id, properties)
            node.update({k: v for k, v in properties.items() if k != "id"})
        return _Result()

    def _clear(self, label: str, node_id: str, key: str) -> _Result:
        node = self.nodes.get((label, node_id))
        if node is not None:
            node.pop(key, None)
        return _Result()


# -- the frozen writers ------------------------------------------------------------------------


def legacy_backfill_governance_status(graph: LegacyWriteTarget, policy_id: str) -> None:
    """The frozen `backfill_governance_status`: three guarded `SET` statements, in this order."""
    graph.query(_BACKFILL_VERSION, params={"policy_id": policy_id})
    graph.query(_BACKFILL_STANDARD, params={"policy_id": policy_id})
    graph.query(_BACKFILL_CONTROL, params={"policy_id": policy_id})


def legacy_cascade_status(graph: LegacyWriteTarget, *, policy_id: str, target_status: str) -> None:
    """The frozen `cascade_status`: one statement over the whole tree."""
    graph.query(_CASCADE, params={"policy_id": policy_id, "target_status": target_status})


def legacy_approve_fork_repoint(
    graph: LegacyWriteTarget,
    *,
    policy_id: str,
    prior_id: str,
    capability_ids: tuple[str, ...],
    target_status: str,
) -> bool:
    """The frozen `approve_fork_repoint`: one guarded statement; `True` when it returned a row."""
    result = graph.query(
        _REPOINT,
        params={
            "policy_id": policy_id,
            "prior_id": prior_id,
            "capability_ids": list(capability_ids),
            "expected": len(capability_ids),
            "target_status": target_status,
        },
    )
    return bool(cast("_Result", result).result_set)


def legacy_create_policy_draft(
    graph: LegacyWriteTarget,
    *,
    policy_id: str,
    title: str,
    owner: tuple[str, str],
    standards: tuple[StandardDraft, ...] = (),
    supersedes_policy_id: str | None = None,
    version: str = "1",
    capability_ids: tuple[str, ...] = (),
) -> bool:
    """The frozen `create_policy_draft`: claim or plain Policy, supersede edge, then children."""
    owner_subject, owner_issuer = owner
    properties = {
        "title": title,
        "status": "draft",
        "version": version,
        "owner_subject": owner_subject,
        "owner_issuer": owner_issuer,
    }
    if capability_ids:
        claimed = graph.query(
            _CLAIM_CREATE,
            params={
                "capability_ids": list(capability_ids),
                "expected": len(capability_ids),
                "policy_id": policy_id,
                "properties": properties,
            },
        )
        if not cast("_Result", claimed).result_set:
            return False
    else:
        graph.query(_PLAIN_CREATE, params={"policy_id": policy_id, "properties": properties})
    if supersedes_policy_id is not None:
        graph.query(_SUPERSEDE, params={"prior_id": supersedes_policy_id, "new_id": policy_id})
    for standard in standards:
        standard_properties: dict[str, object] = dict(standard.extra_properties)
        standard_properties["title"] = standard.title
        standard_properties["status"] = "draft"
        graph.query(
            _STANDARD_CHILD,
            params={
                "policy_id": policy_id,
                "standard_id": standard.id,
                "properties": standard_properties,
            },
        )
        for control in standard.controls:
            control_properties: dict[str, object] = dict(control.extra_properties)
            control_properties["title"] = control.title
            control_properties["type"] = control.control_type
            control_properties["status"] = "draft"
            graph.query(
                _CONTROL_CHILD,
                params={
                    "standard_id": standard.id,
                    "control_id": control.id,
                    "properties": control_properties,
                },
            )
    return True


def _patch(
    graph: LegacyWriteTarget,
    set_statement: str,
    clear_statement: Callable[[str], str],
    id_param: str,
    node_id: str,
    properties: Mapping[str, object],
) -> None:
    set_properties = {key: value for key, value in properties.items() if value is not None}
    null_keys = [key for key, value in properties.items() if value is None]
    if set_properties:
        graph.query(set_statement, params={id_param: node_id, "set_properties": set_properties})
    for key in null_keys:
        graph.query(clear_statement(key), params={id_param: node_id})


def legacy_update_policy_fields(
    graph: LegacyWriteTarget, *, policy_id: str, properties: Mapping[str, object]
) -> None:
    """The frozen `update_policy_fields`: one `+=` for the set keys, then one `= null` per key."""
    _patch(graph, _UPDATE_POLICY_SET, _clear_policy, "policy_id", policy_id, properties)


def legacy_update_standard_fields(
    graph: LegacyWriteTarget, *, standard_id: str, properties: Mapping[str, object]
) -> None:
    """The frozen `update_standard_fields`."""
    _patch(graph, _UPDATE_STANDARD_SET, _clear_standard, "standard_id", standard_id, properties)


def legacy_update_control_fields(
    graph: LegacyWriteTarget, *, control_id: str, properties: Mapping[str, object]
) -> None:
    """The frozen `update_control_fields`."""
    _patch(graph, _UPDATE_CONTROL_SET, _clear_control, "control_id", control_id, properties)


def legacy_add_standard_to_policy(
    graph: LegacyWriteTarget,
    *,
    policy_id: str,
    standard_id: str,
    title: str,
    extra_properties: Mapping[str, object],
) -> None:
    """The frozen `add_standard_to_policy`: defaults, forced `status`, one `MERGE`."""
    properties: dict[str, object] = {
        key: value for key, value in extra_properties.items() if value is not None
    }
    properties.setdefault("implementation_status", "draft")
    properties["title"] = title
    properties["status"] = "draft"
    graph.query(
        _STANDARD_CHILD,
        params={"policy_id": policy_id, "standard_id": standard_id, "properties": properties},
    )


def legacy_add_control_to_standard(
    graph: LegacyWriteTarget,
    *,
    standard_id: str,
    control_id: str,
    title: str,
    control_type: str,
    extra_properties: Mapping[str, object],
) -> None:
    """The frozen `add_control_to_standard`: defaults, forced `type` and `status`, one `MERGE`."""
    properties: dict[str, object] = {
        key: value for key, value in extra_properties.items() if value is not None
    }
    properties.setdefault("implementation_status", "planned")
    properties["title"] = title
    properties["type"] = control_type
    properties["status"] = "draft"
    graph.query(
        _CONTROL_CHILD,
        params={"standard_id": standard_id, "control_id": control_id, "properties": properties},
    )
