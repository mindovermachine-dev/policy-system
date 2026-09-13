"""Tests for `ps_service.company_merge.graph_writer.
persist_role_and_requirement_passthrough`/`persist_obligation_passthrough`
(PLAN_REVIEWED.md §10 Increment 10; issue #28 AC-BI-006 fix): the
Regulation/Role/Requirement/Obligation writers. RegulatoryInstrument keeps
an unconditional `SET` (never canonically deduped, always refreshed);
Role/Requirement/Obligation use `MERGE ... ON CREATE SET`, matching
Capability's own load-bearing pattern (`persist_canonical_nodes`) exactly,
since issue #28's live verification (IMPL_SLICE_7.md) found a real,
pre-existing `policy_system` graph had 202 Role/Requirement/Obligation
nodes' properties silently overwritten by a second `merge_baseline_graph`
run against baseline graphs whose content had drifted slightly.

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.company_merge.falkordb_client`) structurally -- no mocking
library, matching L2 Testing Patterns' "mock at component boundaries" and
this issue's binding testing convention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from ps_service.company_merge.graph_writer import (
    persist_obligation_passthrough,
    persist_role_and_requirement_passthrough,
)
from ps_service.company_merge.models import BaselineNode, ProvenanceEdge


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    """Satisfies `GraphHandle` structurally, capturing every `(query,
    params)` call for assertion.
    """

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


_NODE_UPSERT_PATTERN = re.compile(
    r"^MERGE \(n:(?P<label>\w+) \{id: \$id\}\) (?P<clause>ON CREATE SET|SET) n \+= \$properties$"
)


class _StatefulFakeGraph:
    """A `GraphHandle` stand-in that actually IMPLEMENTS FalkorDB's own
    `MERGE ... SET` vs `MERGE ... ON CREATE SET` property-write semantics,
    unlike `_FakeGraph` above (which only records calls verbatim for
    query/params assertions).

    Keyed on `(label, id)`: an unconditional `SET` always (over)writes the
    node's properties; `ON CREATE SET` writes them only the first time a
    given `(label, id)` is seen, exactly mirroring the database-engine
    guarantee `persist_canonical_nodes`'s docstring describes for
    Capability. Used to prove, at the unit level with no real FalkorDB,
    that a second `persist_*_passthrough` call with DIFFERENT property
    values never disturbs a pre-existing node -- the real, live AC-BI-006
    violation this test file's docstring describes (issue #28).
    """

    def __init__(self) -> None:
        self._properties_by_label_and_id: dict[tuple[str, str], dict[str, object]] = {}

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        match = _NODE_UPSERT_PATTERN.match(q)
        if match is None:
            # Edge/instrument writes this file's tests never assert on the
            # resulting state of -- record nothing, just succeed.
            return _FakeQueryResult([[0]])
        assert params is not None
        label = match.group("label")
        node_id = cast("str", params["id"])
        key = (label, node_id)
        already_exists = key in self._properties_by_label_and_id
        if match.group("clause") == "SET" or not already_exists:
            self._properties_by_label_and_id[key] = dict(
                cast("dict[str, object]", params["properties"])
            )
        return _FakeQueryResult([[0]])

    def properties_of(self, label: str, node_id: str) -> dict[str, object]:
        return self._properties_by_label_and_id[(label, node_id)]


def _role_node() -> BaselineNode:
    return BaselineNode(
        id="role_manufacturer_abc123", properties={"name": "Manufacturer", "confidence": 0.9}
    )


def _requirement_node() -> BaselineNode:
    return BaselineNode(
        id="CRA-1.0_req_art_13.1",
        properties={
            "text": "Conduct a cybersecurity risk assessment.",
            "type": "requirement",
            "confidence": 0.9,
            "role_id": "role_manufacturer_abc123",
        },
    )


def _regulatory_instrument_properties() -> dict[str, object]:
    return {"title": "Cyber Resilience Act", "jurisdiction": "EU"}


def test_persist_writes_regulation_role_and_requirement_with_edges() -> None:
    graph = _FakeGraph()
    role = _role_node()
    requirement = _requirement_node()
    defines_edge = ProvenanceEdge(
        relationship_type="DEFINES", target_id=role.id, source_ref="Art. 13(1)"
    )
    expresses_edge = ProvenanceEdge(
        relationship_type="EXPRESSES", target_id=requirement.id, source_ref="Art. 13(1)"
    )

    persist_role_and_requirement_passthrough(
        graph,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (role,),
        (requirement,),
        (defines_edge, expresses_edge),
    )

    assert len(graph.calls) == 5
    regulatory_instrument_call, role_call, requirement_call, defines_call, expresses_call = (
        graph.calls
    )

    assert (
        regulatory_instrument_call.query
        == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"
    )
    assert regulatory_instrument_call.params == {
        "id": "CRA-1.0",
        "properties": _regulatory_instrument_properties(),
    }

    assert role_call.query == "MERGE (n:Role {id: $id}) ON CREATE SET n += $properties"
    assert role_call.params == {"id": role.id, "properties": role.properties}

    assert (
        requirement_call.query == "MERGE (n:Requirement {id: $id}) ON CREATE SET n += $properties"
    )
    assert requirement_call.params == {"id": requirement.id, "properties": requirement.properties}

    assert defines_call.query == (
        "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), (n:Role {id: $target_id}) "
        "MERGE (r)-[e:DEFINES]->(n) SET e.source_ref = $source_ref"
    )
    assert defines_call.params == {
        "regulatory_instrument_id": "CRA-1.0",
        "target_id": role.id,
        "source_ref": "Art. 13(1)",
    }

    assert expresses_call.query == (
        "MATCH (r:RegulatoryInstrument {id: $regulatory_instrument_id}), "
        "(n:Requirement {id: $target_id}) "
        "MERGE (r)-[e:EXPRESSES]->(n) SET e.source_ref = $source_ref"
    )
    assert expresses_call.params == {
        "regulatory_instrument_id": "CRA-1.0",
        "target_id": requirement.id,
        "source_ref": "Art. 13(1)",
    }


def test_persist_passthrough_writes_instrument_type_verbatim() -> None:
    """AC-BI-011 (Company Merge, write side): `instrument_type` rides
    through the Regulation MERGE verbatim inside `params["properties"]` —
    `regulatory_instrument_properties` is `dict[str, object]` with no key allow-list,
    so the key propagates by construction with NO src change.
    """
    graph = _FakeGraph()

    persist_role_and_requirement_passthrough(
        graph,
        "NIS2-1.0",
        {"title": "NIS2 Directive", "jurisdiction": "EU", "instrument_type": "directive"},
        (),
        (),
        (),
    )

    assert len(graph.calls) == 1
    regulatory_instrument_call = graph.calls[0]
    assert (
        regulatory_instrument_call.query
        == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"
    )
    assert regulatory_instrument_call.params is not None
    properties = regulatory_instrument_call.params["properties"]
    assert isinstance(properties, dict)
    assert properties["instrument_type"] == "directive"


def test_persist_is_idempotent_across_repeated_calls() -> None:
    """Re-running with identical input twice against the same fake graph
    produces the same two sets of calls each time (trivial idempotency
    shape check -- PLAN_REVIEWED.md §10 Increment 10).
    """
    graph = _FakeGraph()
    role = _role_node()
    requirement = _requirement_node()
    defines_edge = ProvenanceEdge(
        relationship_type="DEFINES", target_id=role.id, source_ref="Art. 13(1)"
    )
    expresses_edge = ProvenanceEdge(
        relationship_type="EXPRESSES", target_id=requirement.id, source_ref="Art. 13(1)"
    )

    for _ in range(2):
        persist_role_and_requirement_passthrough(
            graph,
            "CRA-1.0",
            _regulatory_instrument_properties(),
            (role,),
            (requirement,),
            (defines_edge, expresses_edge),
        )

    assert len(graph.calls) == 10
    first_run, second_run = graph.calls[:5], graph.calls[5:]
    assert first_run == second_run


def test_persist_with_no_role_or_requirement_nodes_writes_only_regulation() -> None:
    graph = _FakeGraph()

    persist_role_and_requirement_passthrough(
        graph, "CRA-1.0", _regulatory_instrument_properties(), (), (), ()
    )

    assert len(graph.calls) == 1
    assert graph.calls[0].query == "MERGE (n:RegulatoryInstrument {id: $id}) SET n += $properties"


def test_persist_role_and_requirement_query_uses_on_create_set() -> None:
    """Issue #28 AC-BI-006 fix: Role/Requirement writes use `MERGE ... ON
    CREATE SET`, matching Capability's own load-bearing pattern exactly --
    NOT the unconditional `SET` RegulatoryInstrument still uses (it is
    never canonically deduped and is always meant to refresh).
    """
    graph = _FakeGraph()

    persist_role_and_requirement_passthrough(
        graph,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (_role_node(),),
        (_requirement_node(),),
        (),
    )

    _regulatory_instrument_call, role_call, requirement_call = graph.calls
    assert role_call.query == "MERGE (n:Role {id: $id}) ON CREATE SET n += $properties"
    assert (
        requirement_call.query == "MERGE (n:Requirement {id: $id}) ON CREATE SET n += $properties"
    )


def test_persist_role_preserves_pre_existing_properties_on_second_write() -> None:
    """Reproduces the real, live AC-BI-006 violation (IMPL_SLICE_7.md): a
    second `merge_baseline_graph` run (e.g. re-merging CRA against a
    baseline graph whose content has since drifted) must never overwrite a
    pre-existing Role node's properties -- e.g. `confidence` silently
    drifting from 0.86 to 0.93, exactly as happened to
    `role_the_obligations_laid_down_in_this_regulation_5e6f1d` in the real
    `policy_system` graph.
    """
    graph = _StatefulFakeGraph()
    role_id = "role_the_obligations_laid_down_in_this_regulation_5e6f1d"
    original_properties: dict[str, str | float] = {"name": "Regulated Entity", "confidence": 0.86}
    drifted_properties: dict[str, str | float] = {"name": "Regulated Entity", "confidence": 0.93}

    persist_role_and_requirement_passthrough(
        graph,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (BaselineNode(role_id, original_properties),),
        (),
        (),
    )
    persist_role_and_requirement_passthrough(
        graph,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (BaselineNode(role_id, drifted_properties),),
        (),
        (),
    )

    assert graph.properties_of("Role", role_id) == original_properties


def test_persist_requirement_preserves_pre_existing_text_on_second_write() -> None:
    """Reproduces the real, live AC-BI-006 violation for a Requirement node
    (IMPL_SLICE_7.md): `CRA-1.0_req_art_4.3`'s `confidence` (0.97->0.92) AND
    `text` (a "Member States " prefix silently added) both changed on a
    second run against real `policy_system`. A second write with different
    property values must leave the pre-existing node untouched.
    """
    graph = _StatefulFakeGraph()
    requirement_id = "CRA-1.0_req_art_4.3"
    original_properties: dict[str, str | float] = {
        "text": "Ensure conformity of the product.",
        "type": "requirement",
        "confidence": 0.97,
    }
    drifted_properties: dict[str, str | float] = {
        "text": "Member States shall ensure conformity of the product.",
        "type": "requirement",
        "confidence": 0.92,
    }

    persist_role_and_requirement_passthrough(
        graph,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (),
        (BaselineNode(requirement_id, original_properties),),
        (),
    )
    persist_role_and_requirement_passthrough(
        graph,
        "CRA-1.0",
        _regulatory_instrument_properties(),
        (),
        (BaselineNode(requirement_id, drifted_properties),),
        (),
    )

    assert graph.properties_of("Requirement", requirement_id) == original_properties


def test_persist_obligation_query_uses_on_create_set() -> None:
    """Issue #28 AC-BI-006 fix: Obligation writes use `MERGE ... ON CREATE
    SET`, matching Role/Requirement/Capability's pattern exactly.
    """
    graph = _FakeGraph()
    obligation = BaselineNode(
        id="obl_ensure_correct_application_of_the_ce_marking_regime_0579d8",
        properties={
            "text": "Ensure correct application of the CE marking regime.",
            "confidence": 0.88,
        },
    )

    persist_obligation_passthrough(graph, (obligation,))

    assert len(graph.calls) == 1
    assert graph.calls[0].query == "MERGE (n:Obligation {id: $id}) ON CREATE SET n += $properties"


def test_persist_obligation_preserves_pre_existing_properties_on_second_write() -> None:
    """Reproduces the real, live AC-BI-006 violation for Obligation nodes
    (IMPL_SLICE_7.md): 199 pre-existing Obligation nodes had `confidence`
    drift between a report's `before`/`after` snapshots (e.g.
    `obl_ensure_correct_application_of_the_ce_marking_regime_0579d8`,
    0.88->0.93) after a second `merge_baseline_graph` run. A second write
    with a different `confidence` value must leave the pre-existing node
    untouched.
    """
    graph = _StatefulFakeGraph()
    obligation_id = "obl_ensure_correct_application_of_the_ce_marking_regime_0579d8"
    original_properties: dict[str, str | float] = {
        "text": "Ensure correct application of the CE marking regime.",
        "confidence": 0.88,
    }
    drifted_properties: dict[str, str | float] = {
        "text": "Ensure correct application of the CE marking regime.",
        "confidence": 0.93,
    }

    persist_obligation_passthrough(graph, (BaselineNode(obligation_id, original_properties),))
    persist_obligation_passthrough(graph, (BaselineNode(obligation_id, drifted_properties),))

    assert graph.properties_of("Obligation", obligation_id) == original_properties
