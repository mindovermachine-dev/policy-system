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
    from company_merge._fakes import MakeEmitter

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
        self._capability_rows = capability_rows
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
        if "(n:Policy) RETURN" in q:
            return _FakeQueryResult(self._policy_rows)
        if "(n:Standard) RETURN" in q:
            return _FakeQueryResult(self._standard_rows)
        if "(n:Control) RETURN" in q:
            return _FakeQueryResult(self._control_rows)
        if "n.role_id" in q:
            return _FakeQueryResult(self._requirement_rows)
        if "n.description" in q:
            return _FakeQueryResult(self._capability_rows)
        if "n.name, n.confidence" in q:
            return _FakeQueryResult(self._role_rows)
        if "(n:Obligation) RETURN" in q:
            return _FakeQueryResult(self._obligation_rows)
        if "(n:RegulatoryInstrument {id: $regulatory_instrument_id}) RETURN n" in q:
            return _FakeQueryResult(
                [[_FakeRegulatoryInstrumentNode(self._regulatory_instrument_properties)]]
            )
        raise AssertionError(f"unexpected query issued: {q!r}")


class _TraversableSingleTenantGraph:
    """A single-tenant graph fake that genuinely interprets and executes
    Cypher against an in-memory node/edge store -- see module docstring.
    """

    _NODE_WRITE_RE = re.compile(
        r"^MERGE \(n:(\w+) \{id: \$id\}\)( ON CREATE)? SET n \+= \$properties$"
    )
    _EDGE_WRITE_RE = re.compile(
        r"^MATCH \(s:(\w+) \{id: \$source_id\}\), \(t:(\w+) \{id: \$target_id\}\) "
        r"MERGE \(s\)-\[:(\w+)\]->\(t\)$"
    )
    _READ_INDEX_RE = re.compile(r"^MATCH \(n:(\w+)\) RETURN n\.id, n\.(\w+), n\.embedding$")
    _BACKFILL_RE = re.compile(
        r"^MATCH \(n:(\w+) \{id: \$id\}\) WHERE n\.embedding IS NULL "
        r"SET n\.embedding = \$embedding$"
    )

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
        self.edges: list[tuple[str, str, str]] = []
        self.calls: list[str] = []

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

        edge_match = self._EDGE_WRITE_RE.match(q)
        if edge_match is not None:
            assert params is not None
            relationship_type = edge_match.group(3)
            triple = (
                relationship_type,
                cast("str", params["source_id"]),
                cast("str", params["target_id"]),
            )
            if triple not in self.edges:
                self.edges.append(triple)
            return _FakeQueryResult([])

        read_index_match = self._READ_INDEX_RE.match(q)
        if read_index_match is not None:
            label, text_property = read_index_match.groups()
            index_rows: list[object] = [
                [node_id, props.get(text_property), props.get("embedding")]
                for node_id, props in self.nodes[label].items()
            ]
            return _FakeQueryResult(index_rows)

        backfill_match = self._BACKFILL_RE.match(q)
        if backfill_match is not None:
            assert params is not None
            label = backfill_match.group(1)
            node = self.nodes[label].get(cast("str", params["id"]))
            if node is not None and node.get("embedding") is None:
                node["embedding"] = params["embedding"]
            return _FakeQueryResult([])

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
            policy = self.nodes["Policy"].get(policy_node_id)
            if policy is None or "Engineering" not in str(policy.get("title", "")):
                continue
            standard_node_id = supported_by.get(policy_node_id)
            if standard_node_id is None:
                continue
            control_node_id = implemented_by.get(standard_node_id)
            if control_node_id is None:
                continue
            capability = self.nodes["Capability"].get(capability_node_id)
            standard = self.nodes["Standard"].get(standard_node_id)
            control = self.nodes["Control"].get(control_node_id)
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
) -> None:
    """S4's closing proof: after `merge_baseline_graph` merges the
    Engineering Practices baseline into `policy_system`, the exact UC-3
    Cypher query from PLAN.md §6 S4 returns the real merged row -- closing
    UC-3 for this instrument.
    """
    emitter, _log_path = make_emitter()
    baseline = _engineering_practices_baseline()
    single_tenant = _TraversableSingleTenantGraph()

    result = merge_baseline_graph(
        _REGULATION_ID,
        baseline_graph=baseline,
        single_tenant_graph=single_tenant,
        embed_model=_MODEL,
        similarity_threshold=_THRESHOLD,
        emitter=emitter,
    )

    assert result.policy_canonical_ids == (policy_id("Engineering Practices Policy"),)

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
