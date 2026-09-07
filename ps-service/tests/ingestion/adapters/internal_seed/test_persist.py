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
