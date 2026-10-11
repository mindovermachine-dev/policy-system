"""The real UC-3 round-trip query proof for issue #54's S4.

PLAN.md §6 S4, red-before-green item 4: after `merge_baseline_graph` merges an
internal-sourced baseline into the single-tenant graph, a Cypher query
shaped exactly like the plan's own UC-3 proof --

    MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy)-[:SUPPORTED_BY]->(s:Standard)
    -[:IMPLEMENTED_BY]->(ctrl:Control)
    WHERE p.title CONTAINS 'Engineering'
    RETURN c.name, p.title, s.title, ctrl.title

-- returns real rows.

Fast-fake variant (this repo's non-live suite never reaches a real FalkorDB
instance): `_TraversableSingleTenantGraph` below is not a scripted/canned
responder like every other fake `GraphHandle` in this package -- it
genuinely interprets the MERGE node/edge Cypher `merge_baseline_graph`'s
real `graph_writer.py` issues (via regex matching the exact shapes those
functions emit) into an in-memory node/edge store, and answers the UC-3
query above by actually traversing that store, filtering by
`p.title CONTAINS 'Engineering'` -- the same computation a real FalkorDB
engine would perform, just executed in Python. This proves `merge_baseline_
graph`'s writes are shaped correctly for the real UC-3 traversal, not merely
that the individual MERGE calls look right in isolation (which
`test_merge_baseline_graph.py`'s param-level assertions already cover).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from graph_gateway._fakes import InMemoryGraph

    from company_merge._fakes import MakeEmitter
    from company_merge._gateway_fakes import MergeRig

from ps_service.company_merge.governance_import import GovernanceImport
from ps_service.company_merge.merge import merge_baseline_graph
from ps_service.domain_mapper.identity import (
    capability_id,
    control_id,
    obligation_id,
    policy_id,
    requirement_id,
    role_id,
    standard_id,
)

_MODEL = "fake-embed-model"
_THRESHOLD = 0.85
_REGULATION_ID = "ENGPRAC-3.0"

_UC3_QUERY = (
    "MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy)-[:SUPPORTED_BY]->(s:Standard)"
    "-[:IMPLEMENTED_BY]->(ctrl:Control) "
    "WHERE p.title CONTAINS 'Engineering' "
    "RETURN c.name, p.title, s.title, ctrl.title"
)


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeRegulatoryInstrumentNode:
    """Satisfies `graph_reader._RegulatoryInstrumentNode` structurally -- only
    `.properties` is ever read.
    """

    def __init__(self, properties: dict[str, object]) -> None:
        self.properties = properties


class _ScriptedBaselineGraph:
    """Answers every one of `read_baseline_graph`'s queries with its own
    scripted row set -- mirrors `test_graph_reader.py`'s `_ScriptedFakeGraph`
    dispatch-by-substring style exactly, extended with governance rows.
    """

    def __init__(
        self,
        *,
        regulatory_instrument_properties: dict[str, object],
        role_rows: list[object],
        requirement_rows: list[object],
        obligation_rows: list[object],
        capability_rows: list[object],
        defines_rows: list[object],
        expresses_rows: list[object],
        has_rows: list[object],
        satisfied_by_rows: list[object],
        requires_rows: list[object],
        policy_rows: list[object],
        standard_rows: list[object],
        control_rows: list[object],
        governed_by_rows: list[object],
        supported_by_rows: list[object],
        implemented_by_rows: list[object],
    ) -> None:
        self._regulatory_instrument_properties = regulatory_instrument_properties
        self._role_rows = role_rows
        self._requirement_rows = requirement_rows
        self._obligation_rows = obligation_rows
        # issue #31 -- mutable, keyed by id, so backfill_incoming_capability_
        # embeddings' write has somewhere to land. A 4-element row (this
        # fixture's pre-#31 shape) is padded with a `None` embedding.
        self._capabilities: dict[str, list[object]] = {}
        for row in capability_rows:
            row_list = list(cast("list[object]", row))
            if len(row_list) == 4:
                row_list.append(None)
            self._capabilities[cast("str", row_list[0])] = row_list
        self._defines_rows = defines_rows
        self._expresses_rows = expresses_rows
        self._has_rows = has_rows
        self._satisfied_by_rows = satisfied_by_rows
        self._requires_rows = requires_rows
        self._policy_rows = policy_rows
        self._standard_rows = standard_rows
        self._control_rows = control_rows
        self._governed_by_rows = governed_by_rows
        self._supported_by_rows = supported_by_rows
        self._implemented_by_rows = implemented_by_rows

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        if "[e:DEFINES]" in q:
            return _FakeQueryResult(self._defines_rows)
        if "[e:EXPRESSES]" in q:
            return _FakeQueryResult(self._expresses_rows)
        if "[:HAS]" in q:
            return _FakeQueryResult(self._has_rows)
        if "[:SATISFIED_BY]" in q:
            return _FakeQueryResult(self._satisfied_by_rows)
        if "[:REQUIRES]" in q:
            return _FakeQueryResult(self._requires_rows)
        if "[:GOVERNED_BY]" in q:
            return _FakeQueryResult(self._governed_by_rows)
        if "[:SUPPORTED_BY]" in q:
            return _FakeQueryResult(self._supported_by_rows)
        if "[:IMPLEMENTED_BY]" in q:
            return _FakeQueryResult(self._implemented_by_rows)
        if "(n:Policy) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(self._policy_rows, ("id", "title", "status", "confidence"))
            )
        if "(n:Standard) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(
                    self._standard_rows,
                    ("id", "title", "implementation_status", "confidence", "description"),
                )
            )
        if "(n:Control) RETURN n" in q:
            return _FakeQueryResult(
                self._node_rows(
                    self._control_rows,
                    ("id", "type", "title", "implementation_status", "confidence", "description"),
                )
            )
        if "(n:PracticeArea) RETURN" in q:
            return _FakeQueryResult([])
        if "(n:RiskPath) RETURN" in q:
            return _FakeQueryResult([])
        if "[:COVERS]" in q or "[:OWNS]" in q or "[:MITIGATED_BY]" in q or "[:VERIFIED_BY]" in q:
            # issue #106 -- this fixture carries no classification-layer
            # content; the four new edge queries `read_baseline_graph` now
            # always issues answer empty (external-baseline shape).
            return _FakeQueryResult([])
        if "n.role_id" in q:
            return _FakeQueryResult(self._requirement_rows)
        if "n.description" in q:
            return _FakeQueryResult([list(row) for row in self._capabilities.values()])
        if "n.name, n.confidence" in q:
            return _FakeQueryResult(self._role_rows)
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult(self._obligation_rows)
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
            )
        if "WHERE n.id IN $ids RETURN n.id, n.embedding IS NULL" in q:
            # issue #31, #210 -- backfill_incoming_capability_embeddings' read of which of its ids
            # exist on THIS (incoming) baseline graph and have no embedding yet (the write is a
            # gateway group on the rig's baseline graph).
            assert params is not None
            return _FakeQueryResult(
                [
                    [i, self._capabilities[i][4] is None]
                    for i in cast("list[str]", params["ids"])
                    if i in self._capabilities
                ]
            )
        raise AssertionError(f"unexpected query issued: {q!r}")

    @staticmethod
    def _node_rows(rows: list[object], keys: tuple[str, ...]) -> list[object]:
        """Governance nodes are read whole (`RETURN n`): one single-node row per scripted row.

        The scripted rows keep their column order; a `None` column is an absent property.
        """
        return [
            [
                _FakeRegulatoryInstrumentNode(
                    {
                        key: value
                        for key, value in zip(keys, cast("list[object]", row), strict=True)
                        if value is not None
                    }
                )
            ]
            for row in rows
        ]


class _TraversableSingleTenantGraph:
    """A single-tenant graph fake that genuinely interprets and executes
    Cypher against an in-memory node/edge store -- see module docstring.
    """

    _NODE_WRITE_RE = re.compile(
        r"^MERGE \(n:(\w+) \{id: \$id\}\)( ON CREATE)? SET n \+= \$properties$"
    )
    _EXISTENCE_RE = re.compile(r"^MATCH \(n:(\w+)\) WHERE n\.id IN \$ids RETURN n\.id$")
    _READ_INDEX_RE = re.compile(r"^MATCH \(n:(\w+)\) RETURN n\.id, n\.(\w+), n\.embedding$")
    _EMBEDDING_STATE_RE = re.compile(
        r"^MATCH \(n:(\w+)\) WHERE n\.id IN \$ids RETURN n\.id, n\.embedding IS NULL$"
    )

    def __init__(self, gateway_graph: InMemoryGraph | None = None) -> None:
        self.nodes: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
        self.calls: list[str] = []
        # issue #210: the nodes the merge writes through the gateway (Capability so far) live in
        # the rig's graph; this fake's own store holds the ones still written with Cypher.
        self._gateway_graph = gateway_graph

    @property
    def edges(self) -> list[tuple[str, str, str]]:
        """The edges the merge's gateway groups wrote, as (type, source id, target id)."""
        if self._gateway_graph is None:
            return []
        return [(key[0], key[2], key[4]) for key in self._gateway_graph.edges]

    def _node(self, label: str, node_id: str) -> dict[str, object] | None:
        """The stored properties of one node: the Cypher-written store, then the gateway's."""
        found = self.nodes[label].get(node_id)
        if found is None and self._gateway_graph is not None:
            found = self._gateway_graph.nodes.get((label, node_id))
        return found

    def holds(self, label: str, node_id: str) -> bool:
        """Whether a `label` node `node_id` exists, however it was written."""
        return self._node(label, node_id) is not None

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)

        if q == _UC3_QUERY:
            return _FakeQueryResult(self._run_uc3())

        node_match = self._NODE_WRITE_RE.match(q)
        if node_match is not None:
            assert params is not None
            label, on_create = node_match.group(1), node_match.group(2) is not None
            node_id = cast("str", params["id"])
            properties = cast("dict[str, object]", params.get("properties", {}))
            self._write_node(label, node_id, properties, on_create=on_create)
            return _FakeQueryResult([])

        existence_match = self._EXISTENCE_RE.match(q)
        if existence_match is not None:
            # issue #210: the existing-node reads (create-only upserts, edge endpoints); every
            # write is a gateway group, so the rig's graph answers
            assert params is not None
            label = existence_match.group(1)
            return _FakeQueryResult(
                [[i] for i in cast("list[str]", params["ids"]) if self.holds(label, i)]
            )

        read_index_match = self._READ_INDEX_RE.match(q)
        if read_index_match is not None:
            label, text_property = read_index_match.groups()
            held = dict(self.nodes[label])
            if self._gateway_graph is not None:
                held.update(
                    {i: p for (lbl, i), p in self._gateway_graph.nodes.items() if lbl == label}
                )
            index_rows: list[object] = [
                [node_id, props.get(text_property), props.get("embedding")]
                for node_id, props in held.items()
            ]
            return _FakeQueryResult(index_rows)

        state_match = self._EMBEDDING_STATE_RE.match(q)
        if state_match is not None:
            # issue #210: the backfill's read of which ids exist and have no embedding (its write
            # is a gateway group on `policy_system`)
            assert params is not None
            label = state_match.group(1)
            return _FakeQueryResult(
                [
                    [i, node.get("embedding") is None]
                    for i in cast("list[str]", params["ids"])
                    if (node := self._node(label, i)) is not None
                ]
            )

        # Provenance-edge / RegulatoryInstrument writes: irrelevant to the
        # UC-3 traversal, safely ignored (never asserted on here).
        return _FakeQueryResult([])

    def _write_node(
        self, label: str, node_id: str, properties: dict[str, object], *, on_create: bool
    ) -> None:
        existing = self.nodes[label].get(node_id)
        if on_create:
            if existing is None:
                self.nodes[label][node_id] = dict(properties)
        else:
            self.nodes[label].setdefault(node_id, {}).update(properties)

    def _run_uc3(self) -> list[object]:
        """Actually traverse the in-memory store for the UC-3 pattern -- not scripted."""
        governed_by = [(s, t) for rel, s, t in self.edges if rel == "GOVERNED_BY"]
        supported_by = {s: t for rel, s, t in self.edges if rel == "SUPPORTED_BY"}
        implemented_by = {s: t for rel, s, t in self.edges if rel == "IMPLEMENTED_BY"}

        rows: list[object] = []
        for capability_node_id, policy_node_id in governed_by:
            policy = self._node("Policy", policy_node_id)
            if policy is None or "Engineering" not in str(policy.get("title", "")):
                continue
            standard_node_id = supported_by.get(policy_node_id)
            if standard_node_id is None:
                continue
            control_node_id = implemented_by.get(standard_node_id)
            if control_node_id is None:
                continue
            capability = self._node("Capability", capability_node_id)
            standard = self._node("Standard", standard_node_id)
            control = self._node("Control", control_node_id)
            if capability is None or standard is None or control is None:
                continue
            rows.append(
                [
                    capability.get("name"),
                    policy.get("title"),
                    standard.get("title"),
                    control.get("title"),
                ]
            )
        return rows


def _engineering_practices_baseline() -> _ScriptedBaselineGraph:
    """One fully-wired internal instrument: Role/Requirement/Obligation/
    Capability spine plus Policy/Standard/Control governance layer, titled
    so the Policy matches the UC-3 query's `WHERE p.title CONTAINS
    'Engineering'` filter.
    """
    rid = _REGULATION_ID
    role_node_id = role_id("Software Engineer", rid)
    requirement_node_id = requirement_id(rid, "1", "1", None)
    obligation_text = "Conduct a peer code review before merging."
    obligation_node_id = obligation_id(role_node_id, obligation_text)
    capability_name = "Code Review Capability"
    capability_node_id = capability_id(capability_name)
    policy_title = "Engineering Practices Policy"
    policy_node_id = policy_id(policy_title)
    standard_node_id = standard_id(policy_node_id, "1")
    control_node_id = control_id(standard_node_id, "manual")

    return _ScriptedBaselineGraph(
        regulatory_instrument_properties={
            "id": rid,
            "title": "Engineering Practices",
            "source_type": "internal",
        },
        role_rows=[[role_node_id, "Software Engineer", 0.9]],
        requirement_rows=[
            [
                requirement_node_id,
                "Must review code before merge.",
                "requirement",
                0.9,
                role_node_id,
            ]
        ],
        obligation_rows=[[obligation_node_id, obligation_text, 0.9]],
        capability_rows=[[capability_node_id, capability_name, 0.85, None]],
        defines_rows=[[role_node_id, "Section 1.1"]],
        expresses_rows=[[requirement_node_id, "Section 1.1"]],
        has_rows=[[role_node_id, obligation_node_id]],
        satisfied_by_rows=[[requirement_node_id, obligation_node_id]],
        requires_rows=[[obligation_node_id, capability_node_id]],
        policy_rows=[[policy_node_id, policy_title, "draft", 0.9]],
        standard_rows=[[standard_node_id, "Code Review Standard", "draft", 0.85, None]],
        control_rows=[[control_node_id, "manual", "Peer Review Control", "planned", 0.8, None]],
        governed_by_rows=[[capability_node_id, policy_node_id]],
        supported_by_rows=[[policy_node_id, standard_node_id]],
        implemented_by_rows=[[standard_node_id, control_node_id]],
    )


def test_uc3_capability_policy_standard_control_query_returns_real_rows(
    make_emitter: MakeEmitter,
    merge_rig: MergeRig,
) -> None:
    """S4's closing proof: after `merge_baseline_graph` merges the
    Engineering Practices baseline into `policy_system`, the exact UC-3
    Cypher query from PLAN.md §6 S4 returns the real merged row -- closing
    UC-3 for this instrument.
    """
    emitter, _log_path = make_emitter()
    baseline = _engineering_practices_baseline()
    single_tenant = _TraversableSingleTenantGraph(merge_rig.policy_system)

    result = merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
        governance=GovernanceImport(owner=("sub-1", "https://issuer.example")),
        **merge_rig.merge_keywords(),
    )

    assert result.governance_policies == 1
    assert single_tenant.holds("Policy", policy_id("Engineering Practices Policy"))

    query_result = single_tenant.query(_UC3_QUERY)
    rows = query_result.result_set

    assert rows == [
        [
            "Code Review Capability",
            "Engineering Practices Policy",
            "Code Review Standard",
            "Peer Review Control",
        ]
    ]
