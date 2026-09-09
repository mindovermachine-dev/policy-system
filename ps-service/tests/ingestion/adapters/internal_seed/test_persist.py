"""Tests for `ps_service.ingestion.adapters.internal_seed.persist`.

S2's red-before-green tests 3-5 (PLAN.md S2): AC-BI-011 (dangling-edge
fail-closed, zero writes), B5 (native verbatim / baseline minted dual write),
D6 (Requirement.role_id omitted, never null, when ambiguous).

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols structurally --
no mocking library, matching L2 Testing Patterns' "mock at component
boundaries" and mirroring `tests/ingestion/test_graph_writer.py`'s own style.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from ps_service.domain_mapper.identity import capability_id, obligation_id, role_id
from ps_service.ingestion.adapters.internal_seed.adapter import InternalSeedIngestionAdapter
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import (
    EdgeType,
    InternalRegulationSeed,
    NodeLabel,
    SeedEdge,
    SeedNode,
)
from ps_service.ingestion.adapters.internal_seed.persist import (
    ingest_internal_regulatory_instrument,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_DANGLING_EDGE_FIXTURE = (
    _REPO_ROOT / "test-data" / "engineering-practices" / "engineering-practices-dangling-edge.json"
)


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    @property
    def result_set(self) -> list[object]:
        return []


class _FakeGraph:
    """Satisfies `GraphHandle` structurally, capturing every `(query, params)` call."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult()


def _ri(instrument_id: str = "TESTREG-1.0") -> SeedNode:
    return SeedNode(
        label="RegulatoryInstrument",
        id=instrument_id,
        properties={
            "title": "Test Regulation",
            "source_type": "internal",
            "effective_date": "2026-01-01",
            "version": "1.0",
            "status": "active",
        },
    )


def _role(local_id: str, name: str) -> SeedNode:
    return SeedNode(label="Role", id=local_id, properties={"name": name})


def _requirement(local_id: str, text: str) -> SeedNode:
    return SeedNode(
        label="Requirement", id=local_id, properties={"text": text, "type": "requirement"}
    )


def _obligation(local_id: str, text: str) -> SeedNode:
    return SeedNode(label="Obligation", id=local_id, properties={"text": text})


def _capability(local_id: str, name: str) -> SeedNode:
    return SeedNode(label="Capability", id=local_id, properties={"name": name})


def _edge(
    edge_type: EdgeType,
    from_label: NodeLabel,
    from_id: str,
    to_label: NodeLabel,
    to_id: str,
    *,
    source_ref: str | None = None,
) -> SeedEdge:
    properties = {"source_ref": source_ref} if source_ref is not None else {}
    # `model_validate` (rather than the keyword constructor) sidesteps a
    # `from_`/`from` alias-vs-field-name mismatch basedpyright's Pydantic
    # model synthesis reports as a false-positive `reportCallIssue`, even
    # though `populate_by_name=True` genuinely accepts `from_=` at runtime.
    return SeedEdge.model_validate(
        {
            "type": edge_type,
            "from": {"label": from_label, "id": from_id},
            "to": {"label": to_label, "id": to_id},
            "properties": properties,
        }
    )


def _build_seed(instrument_id: str = "TESTREG-1.0") -> InternalRegulationSeed:
    """A small, self-consistent seed: two Roles, two Requirements, two Obligations, one
    Capability -- `req-2` is `SATISFIED_BY` two Obligations borne by *different* Roles
    (D6's ambiguous case), `req-1` by exactly one (D6's resolvable case).
    """
    nodes = (
        _ri(instrument_id),
        _role("role-a", "Role A"),
        _role("role-b", "Role B"),
        _requirement("req-1", "Requirement text one"),
        _requirement("req-2", "Requirement text two"),
        _obligation("obl-a", "Do A"),
        _obligation("obl-b", "Do B"),
        _capability("cap-1", "Capability One"),
    )
    edges = (
        _edge("DEFINES", "RegulatoryInstrument", instrument_id, "Role", "role-a", source_ref="d1"),
        _edge("DEFINES", "RegulatoryInstrument", instrument_id, "Role", "role-b", source_ref="d2"),
        _edge(
            "EXPRESSES",
            "RegulatoryInstrument",
            instrument_id,
            "Requirement",
            "req-1",
            source_ref="1.1",
        ),
        _edge(
            "EXPRESSES",
            "RegulatoryInstrument",
            instrument_id,
            "Requirement",
            "req-2",
            source_ref="1.2",
        ),
        _edge("HAS", "Role", "role-a", "Obligation", "obl-a"),
        _edge("HAS", "Role", "role-b", "Obligation", "obl-b"),
        _edge("SATISFIED_BY", "Requirement", "req-1", "Obligation", "obl-a"),
        _edge("SATISFIED_BY", "Requirement", "req-2", "Obligation", "obl-a"),
        _edge("SATISFIED_BY", "Requirement", "req-2", "Obligation", "obl-b"),
        _edge("REQUIRES", "Obligation", "obl-a", "Capability", "cap-1"),
        _edge("REQUIRES", "Obligation", "obl-b", "Capability", "cap-1"),
    )
    return InternalRegulationSeed(nodes=nodes, edges=edges)


def _written_node_ids(graph: _FakeGraph, *, label_prefix: str = "MERGE (n:") -> set[str]:
    """The `id` param of every `MERGE (n:...)` node write recorded on `graph`."""
    return {
        cast("str", call.params["id"])
        for call in graph.calls
        if call.params is not None and label_prefix in call.query
    }


def _written_node_properties(
    graph: _FakeGraph, *, label_prefix: str
) -> dict[str, dict[str, object]]:
    """`{written id: written properties}` for every `MERGE (n:{label_prefix}...)` write."""
    return {
        cast("str", call.params["id"]): cast("dict[str, object]", call.params["properties"])
        for call in graph.calls
        if call.params is not None and label_prefix in call.query
    }


def test_dangling_requires_edge_fails_closed_no_partial_write() -> None:
    """AC-BI-011: a dangling REQUIRES edge raises `InternalSeedError` with zero writes.

    Uses the dedicated `engineering-practices-dangling-edge.json` fixture
    (B1) -- schema-clean at the JSON-Schema layer, but referentially invalid
    (its one REQUIRES edge targets a Capability id never declared as a node).
    """
    seed = InternalSeedIngestionAdapter().read_seed(str(_DANGLING_EDGE_FIXTURE))
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    with pytest.raises(InternalSeedError):
        ingest_internal_regulatory_instrument(
            seed, baseline_graph=baseline_graph, native_graph=native_graph
        )

    assert baseline_graph.calls == []
    assert native_graph.calls == []


def test_persists_native_verbatim_and_baseline_minted() -> None:
    """B5: `{short}_native` gets the raw local-id submission; `{short}_baseline` gets
    the minted canonical spine -- independently asserted node counts/shapes.
    """
    seed = _build_seed()
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    result = ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    assert result.regulatory_instrument_id == "TESTREG-1.0"
    assert result.role_count == 2
    assert result.requirement_count == 2
    assert result.obligation_count == 2
    assert result.capability_count == 1

    # Native: local ids, verbatim, no minting.
    native_node_ids = _written_node_ids(native_graph)
    assert native_node_ids == {
        "TESTREG-1.0",
        "role-a",
        "role-b",
        "req-1",
        "req-2",
        "obl-a",
        "obl-b",
        "cap-1",
    }

    # Baseline: canonical ids, minted via the same formulas domain_mapper.identity exports.
    expected_role_a = role_id("Role A", "TESTREG-1.0")
    expected_role_b = role_id("Role B", "TESTREG-1.0")
    expected_capability = capability_id("Capability One")
    expected_obligation_a = obligation_id(expected_role_a, "Do A")
    expected_obligation_b = obligation_id(expected_role_b, "Do B")

    baseline_node_ids = _written_node_ids(baseline_graph)
    requirement_ids = {node_id for node_id in baseline_node_ids if "_req_" in node_id}
    assert len(requirement_ids) == 2
    assert all(node_id.startswith("TESTREG-1.0_req_") for node_id in requirement_ids)
    assert baseline_node_ids - requirement_ids == {
        "TESTREG-1.0",
        expected_role_a,
        expected_role_b,
        expected_capability,
        expected_obligation_a,
        expected_obligation_b,
    }
    # Local ids never leak into the baseline graph.
    assert "role-a" not in baseline_node_ids
    assert "obl-a" not in baseline_node_ids
    assert "cap-1" not in baseline_node_ids


def _policy(local_id: str, title: str, *, status: str = "draft") -> SeedNode:
    return SeedNode(label="Policy", id=local_id, properties={"title": title, "status": status})


def _standard(local_id: str, title: str, *, implementation_status: str = "draft") -> SeedNode:
    return SeedNode(
        label="Standard",
        id=local_id,
        properties={"title": title, "implementation_status": implementation_status},
    )


def _control(
    local_id: str,
    title: str,
    *,
    control_type: str = "automated",
    implementation_status: str = "planned",
    extra_properties: dict[str, str] | None = None,
) -> SeedNode:
    properties: dict[str, str | float] = {
        "type": control_type,
        "title": title,
        "implementation_status": implementation_status,
    }
    if extra_properties:
        properties.update(extra_properties)
    return SeedNode(label="Control", id=local_id, properties=properties)


def test_persists_authored_policy_node_and_governed_by_edge_verbatim() -> None:
    """GH #76 AC-BI-004 (Policy portion): a submitted Policy + GOVERNED_BY edge
    from a Capability persists verbatim, and `InternalIngestResult.policy_count == 1`.
    """
    seed = _build_seed()
    nodes = (*seed.nodes, _policy("pol-1", "Access Control Policy"))
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    result = ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    assert result.policy_count == 1
    from ps_service.domain_mapper.identity import policy_id

    expected_policy_id = policy_id("Access Control Policy")
    policy_writes = _written_node_properties(baseline_graph, label_prefix="MERGE (n:Policy")
    assert policy_writes == {
        expected_policy_id: {"title": "Access Control Policy", "status": "draft"}
    }


def test_seed_with_no_policy_nodes_still_succeeds_with_zero_policy_count() -> None:
    """AC-BI-005: omitting Policy/GOVERNED_BY entirely still succeeds, persisting
    only the regulatory spine, with zero governance nodes invented in their place.
    """
    seed = _build_seed()
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    result = ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    assert result.policy_count == 0
    policy_writes = _written_node_properties(baseline_graph, label_prefix="MERGE (n:Policy")
    assert policy_writes == {}


def test_capability_with_two_governed_by_edges_fails_closed_no_partial_write() -> None:
    """AC-BI-008/009 for GOVERNED_BY: a Capability with two outbound GOVERNED_BY
    edges raises `InternalSeedError` naming the Capability, zero writes.
    """
    seed = _build_seed()
    nodes = (
        *seed.nodes,
        _policy("pol-1", "Policy One"),
        _policy("pol-2", "Policy Two"),
    )
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-2"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    with pytest.raises(InternalSeedError, match="cap-1"):
        ingest_internal_regulatory_instrument(
            seed, baseline_graph=baseline_graph, native_graph=native_graph
        )

    assert baseline_graph.calls == []
    assert native_graph.calls == []


def test_dangling_governed_by_edge_fails_closed() -> None:
    """A GOVERNED_BY edge referencing an undeclared Policy id raises, zero writes."""
    seed = _build_seed()
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-does-not-exist"),
    )
    seed = InternalRegulationSeed(nodes=seed.nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    with pytest.raises(InternalSeedError):
        ingest_internal_regulatory_instrument(
            seed, baseline_graph=baseline_graph, native_graph=native_graph
        )

    assert baseline_graph.calls == []
    assert native_graph.calls == []


def test_persists_authored_standard_node_and_supported_by_edge_verbatim() -> None:
    """GH #76 AC-BI-004 (Standard portion): a submitted Standard + SUPPORTED_BY edge
    from a Policy persists verbatim, and `InternalIngestResult.standard_count == 1`.
    """
    seed = _build_seed()
    nodes = (
        *seed.nodes,
        _policy("pol-1", "Access Control Policy"),
        _standard("std-1", "Access Control Standard"),
    )
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-1"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    result = ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    assert result.standard_count == 1
    from ps_service.domain_mapper.identity import policy_id, standard_id

    expected_policy_id = policy_id("Access Control Policy")
    expected_standard_id = standard_id(expected_policy_id, "Access Control Standard")
    standard_writes = _written_node_properties(baseline_graph, label_prefix="MERGE (n:Standard")
    assert standard_writes == {
        expected_standard_id: {
            "title": "Access Control Standard",
            "implementation_status": "draft",
        }
    }


@pytest.mark.parametrize("supported_by_count", [0, 2])
def test_standard_with_zero_or_two_supported_by_edges_fails_closed_no_partial_write(
    supported_by_count: int,
) -> None:
    """AC-BI-008/009 for SUPPORTED_BY: a Standard with zero or two-or-more inbound
    SUPPORTED_BY edges raises `InternalSeedError` naming the Standard, zero writes.
    """
    seed = _build_seed()
    nodes = (
        *seed.nodes,
        _policy("pol-1", "Policy One"),
        _policy("pol-2", "Policy Two"),
        _standard("std-1", "Standard One"),
    )
    supporting_edges = tuple(
        _edge("SUPPORTED_BY", "Policy", policy_local_id, "Standard", "std-1")
        for policy_local_id in ("pol-1", "pol-2")[:supported_by_count]
    )
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        *supporting_edges,
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    with pytest.raises(InternalSeedError, match="std-1"):
        ingest_internal_regulatory_instrument(
            seed, baseline_graph=baseline_graph, native_graph=native_graph
        )

    assert baseline_graph.calls == []
    assert native_graph.calls == []


def test_dangling_supported_by_edge_fails_closed() -> None:
    """A SUPPORTED_BY edge referencing an undeclared Standard id raises, zero writes."""
    seed = _build_seed()
    nodes = (*seed.nodes, _policy("pol-1", "Policy One"))
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-does-not-exist"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    with pytest.raises(InternalSeedError):
        ingest_internal_regulatory_instrument(
            seed, baseline_graph=baseline_graph, native_graph=native_graph
        )

    assert baseline_graph.calls == []
    assert native_graph.calls == []


def test_policy_with_two_supported_by_edges_persists_two_distinct_standards() -> None:
    """AC-BI-006's literal proof: one Policy, two SUPPORTED_BY edges to two
    differently-titled Standards -- both persist as distinct nodes with distinct ids.
    """
    seed = _build_seed()
    nodes = (
        *seed.nodes,
        _policy("pol-1", "Access Control Policy"),
        _standard("std-1", "Standard A"),
        _standard("std-2", "Standard B"),
    )
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-2"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    result = ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    assert result.standard_count == 2
    from ps_service.domain_mapper.identity import policy_id, standard_id

    expected_policy_id = policy_id("Access Control Policy")
    expected_a = standard_id(expected_policy_id, "Standard A")
    expected_b = standard_id(expected_policy_id, "Standard B")
    assert expected_a != expected_b

    standard_ids = _written_node_ids(baseline_graph, label_prefix="MERGE (n:Standard")
    assert standard_ids == {expected_a, expected_b}


def test_persists_authored_control_node_and_implemented_by_edge_verbatim() -> None:
    """GH #76 AC-BI-004 (Control portion): a submitted Control + IMPLEMENTED_BY edge
    from a Standard persists verbatim (including optional operational fields), and
    `InternalIngestResult.control_count == 1`.
    """
    seed = _build_seed()
    nodes = (
        *seed.nodes,
        _policy("pol-1", "Access Control Policy"),
        _standard("std-1", "Access Control Standard"),
        _control(
            "ctrl-1",
            "Automated Access Review Check",
            implementation_status="implemented",
            extra_properties={
                "description": "Nightly automated review of privileged access grants.",
                "execution_frequency": "daily",
                "last_test_date": "2026-08-01",
                "next_review_date": "2026-11-01",
                "evidence_ref": "evidence://access-review/2026-08-01",
            },
        ),
    )
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-1"),
        _edge("IMPLEMENTED_BY", "Standard", "std-1", "Control", "ctrl-1"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    result = ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    assert result.control_count == 1
    from ps_service.domain_mapper.identity import control_id, policy_id, standard_id

    expected_policy_id = policy_id("Access Control Policy")
    expected_standard_id = standard_id(expected_policy_id, "Access Control Standard")
    expected_control_id = control_id(expected_standard_id, "Automated Access Review Check")
    control_writes = _written_node_properties(baseline_graph, label_prefix="MERGE (n:Control")
    assert control_writes == {
        expected_control_id: {
            "type": "automated",
            "title": "Automated Access Review Check",
            "implementation_status": "implemented",
            "description": "Nightly automated review of privileged access grants.",
            "execution_frequency": "daily",
            "last_test_date": "2026-08-01",
            "next_review_date": "2026-11-01",
            "evidence_ref": "evidence://access-review/2026-08-01",
        }
    }


@pytest.mark.parametrize("implemented_by_count", [0, 2])
def test_control_with_zero_or_two_implemented_by_edges_fails_closed_no_partial_write(
    implemented_by_count: int,
) -> None:
    """AC-BI-008/009 for IMPLEMENTED_BY: a Control with zero or two-or-more inbound
    IMPLEMENTED_BY edges raises `InternalSeedError` naming the Control, zero writes.
    """
    seed = _build_seed()
    nodes = (
        *seed.nodes,
        _policy("pol-1", "Policy One"),
        _standard("std-1", "Standard One"),
        _standard("std-2", "Standard Two"),
        _control("ctrl-1", "Control One"),
    )
    implementing_edges = tuple(
        _edge("IMPLEMENTED_BY", "Standard", standard_local_id, "Control", "ctrl-1")
        for standard_local_id in ("std-1", "std-2")[:implemented_by_count]
    )
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-2"),
        *implementing_edges,
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    with pytest.raises(InternalSeedError, match="ctrl-1"):
        ingest_internal_regulatory_instrument(
            seed, baseline_graph=baseline_graph, native_graph=native_graph
        )

    assert baseline_graph.calls == []
    assert native_graph.calls == []


def test_dangling_implemented_by_edge_fails_closed() -> None:
    """An IMPLEMENTED_BY edge referencing an undeclared Control id raises, zero writes."""
    seed = _build_seed()
    nodes = (*seed.nodes, _policy("pol-1", "Policy One"), _standard("std-1", "Standard One"))
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-1"),
        _edge("IMPLEMENTED_BY", "Standard", "std-1", "Control", "ctrl-does-not-exist"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    with pytest.raises(InternalSeedError):
        ingest_internal_regulatory_instrument(
            seed, baseline_graph=baseline_graph, native_graph=native_graph
        )

    assert baseline_graph.calls == []
    assert native_graph.calls == []


def test_standard_with_two_implemented_by_edges_same_type_persists_two_distinct_controls() -> None:
    """AC-BI-007's literal proof: one Standard, two IMPLEMENTED_BY edges to two Controls
    both `type: "automated"` but differently titled -- both persist as distinct nodes
    with distinct ids.

    This is the test that would have been silently broken -- one Control clobbering
    the other via `MERGE` -- before the AC-BI-003 identity fix (the old
    `control_id(standard_node_id, control_type)` formula keyed identity on `type`
    alone, so two same-typed Controls under the same Standard collided onto one
    node). Confirmed as a genuine regression test (not a tautology) by running it
    against the pre-fix `control_id` first -- see IMPL_SLICE_3.md's evidence block
    for the red run.
    """
    seed = _build_seed()
    nodes = (
        *seed.nodes,
        _policy("pol-1", "Access Control Policy"),
        _standard("std-1", "Access Control Standard"),
        _control("ctrl-1", "Control A", control_type="automated"),
        _control("ctrl-2", "Control B", control_type="automated"),
    )
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-1"),
        _edge("IMPLEMENTED_BY", "Standard", "std-1", "Control", "ctrl-1"),
        _edge("IMPLEMENTED_BY", "Standard", "std-1", "Control", "ctrl-2"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    result = ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    assert result.control_count == 2
    from ps_service.domain_mapper.identity import control_id, policy_id, standard_id

    expected_policy_id = policy_id("Access Control Policy")
    expected_standard_id = standard_id(expected_policy_id, "Access Control Standard")
    expected_a = control_id(expected_standard_id, "Control A")
    expected_b = control_id(expected_standard_id, "Control B")
    assert expected_a != expected_b

    control_ids = _written_node_ids(baseline_graph, label_prefix="MERGE (n:Control")
    assert control_ids == {expected_a, expected_b}


def test_full_capability_policy_standard_control_chain_persists_every_node_and_edge() -> None:
    """End-to-end: one document submitting the complete Capability -> Policy ->
    Standard -> Control chain -- every node and edge persists, and
    `policy_count == standard_count == control_count == 1` (mirrors
    `ps-domain-concepts.md`'s own Example 3 shape, minus `confidence`).
    """
    seed = _build_seed()
    nodes = (
        *seed.nodes,
        _policy("pol-1", "Access Control Policy"),
        _standard("std-1", "Access Control Standard"),
        _control("ctrl-1", "Automated Access Review Check"),
    )
    edges = (
        *seed.edges,
        _edge("GOVERNED_BY", "Capability", "cap-1", "Policy", "pol-1"),
        _edge("SUPPORTED_BY", "Policy", "pol-1", "Standard", "std-1"),
        _edge("IMPLEMENTED_BY", "Standard", "std-1", "Control", "ctrl-1"),
    )
    seed = InternalRegulationSeed(nodes=nodes, edges=edges)
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    result = ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    assert result.policy_count == 1
    assert result.standard_count == 1
    assert result.control_count == 1

    from ps_service.domain_mapper.identity import control_id, policy_id, standard_id

    expected_policy_id = policy_id("Access Control Policy")
    expected_standard_id = standard_id(expected_policy_id, "Access Control Standard")
    expected_control_id = control_id(expected_standard_id, "Automated Access Review Check")

    baseline_node_ids = _written_node_ids(baseline_graph)
    assert {expected_policy_id, expected_standard_id, expected_control_id} <= baseline_node_ids

    governed_by_calls = [
        call for call in baseline_graph.calls if "MERGE (a)-[r:GOVERNED_BY]->(b)" in call.query
    ]
    supported_by_calls = [
        call for call in baseline_graph.calls if "MERGE (a)-[r:SUPPORTED_BY]->(b)" in call.query
    ]
    implemented_by_calls = [
        call for call in baseline_graph.calls if "MERGE (a)-[r:IMPLEMENTED_BY]->(b)" in call.query
    ]
    assert len(governed_by_calls) == 1
    assert len(supported_by_calls) == 1
    assert len(implemented_by_calls) == 1

    native_node_ids = _written_node_ids(native_graph)
    assert {"pol-1", "std-1", "ctrl-1"} <= native_node_ids


def test_role_id_omitted_when_ambiguous_never_null() -> None:
    """D6: `req-1` (satisfied by one Role's Obligation) gets `role_id` set;
    `req-2` (satisfied by two different Roles' Obligations) omits it entirely --
    never writes a literal `None`/`null`.
    """
    seed = _build_seed()
    baseline_graph = _FakeGraph()
    native_graph = _FakeGraph()

    ingest_internal_regulatory_instrument(
        seed, baseline_graph=baseline_graph, native_graph=native_graph
    )

    requirement_writes = _written_node_properties(
        baseline_graph, label_prefix="MERGE (n:Requirement"
    )
    assert len(requirement_writes) == 2

    resolvable_properties = next(
        properties for node_id, properties in requirement_writes.items() if node_id.endswith("_1_1")
    )
    ambiguous_properties = next(
        properties for node_id, properties in requirement_writes.items() if node_id.endswith("_1_2")
    )

    assert "role_id" in resolvable_properties
    assert resolvable_properties["role_id"] == role_id("Role A", "TESTREG-1.0")

    assert "role_id" not in ambiguous_properties
    for properties in requirement_writes.values():
        assert None not in properties.values()
