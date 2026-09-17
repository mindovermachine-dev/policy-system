"""Tests for `ps_service.company_merge.graph_writer.validate_classification_edge_endpoints`
and `.classification_write_counts` (issue #106, AC-BI-008/AC-BI-011).

`validate_classification_edge_endpoints` is a genuinely new mechanism in this
module (PLAN.md §4.3 point 4): every other validation function here only
ever checks in-memory dict membership; this one issues a real (batched) read
against `single_tenant_graph` for whichever endpoints have no
`canonical_id_by_incoming_id` entry, since PracticeArea/RiskPath sources and
`VERIFIED_BY`'s Control target are passthrough nodes with no dedup
resolution to look up. Fakes mirror `test_graph_writer_edge_rewiring.py`'s
`_FakeGraph` convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_writer import (
    classification_write_counts,
    validate_classification_edge_endpoints,
)
from ps_service.company_merge.models import BareEdge, BaselineGraph, BaselineNode


@dataclass
class _RecordedCall:
    query: str
    params: dict[str, object] | None


class _FakeQueryResult:
    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


class _FakeGraph:
    """Satisfies `GraphHandle` structurally: answers the one `UNWIND ...
    MATCH (n {id: id}) RETURN id` query this module issues with whichever of
    `existing_ids` the caller asked about, and records every call.
    """

    def __init__(self, *, existing_ids: frozenset[str] = frozenset()) -> None:
        self._existing_ids = existing_ids
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        assert q == "UNWIND $ids AS id MATCH (n {id: id}) RETURN id"
        assert params is not None
        requested_ids = cast("list[str]", params["ids"])
        return _FakeQueryResult([[rid] for rid in requested_ids if rid in self._existing_ids])


def test_no_unresolved_endpoints_issues_zero_calls() -> None:
    """Every endpoint already has a `canonical_id_by_incoming_id` entry (the
    Capability/Policy dedup-resolved case) -- no existence read is needed at
    all, mirroring `persist_practice_area_and_risk_path_passthrough`'s own
    "nothing to check" structural no-op reasoning.
    """
    graph = _FakeGraph()
    edge = BareEdge(relationship_type="COVERS", source_id="pa_x", target_id="cap_x")
    canonical_id_by_incoming_id = {"pa_x": "pa_x", "cap_x": "cap_x"}

    validate_classification_edge_endpoints(graph, (edge,), canonical_id_by_incoming_id)

    assert graph.calls == []


def test_empty_classification_edges_issues_zero_calls() -> None:
    """AC-BI-009's structural no-op, at this function's own level: an empty
    `classification_edges` tuple has no endpoints to resolve.
    """
    graph = _FakeGraph()

    validate_classification_edge_endpoints(graph, (), {})

    assert graph.calls == []


def test_unresolved_endpoint_found_in_single_tenant_graph_does_not_raise() -> None:
    """A PracticeArea source (never in `canonical_id_by_incoming_id` by
    design) that DOES already exist in `single_tenant_graph` -- because the
    node passthrough call already persisted it earlier in this same call --
    passes validation with exactly one batched read issued.
    """
    graph = _FakeGraph(existing_ids=frozenset({"pa_secure_sdlc_4a7c1d"}))
    edge = BareEdge(
        relationship_type="COVERS", source_id="pa_secure_sdlc_4a7c1d", target_id="cap_x"
    )
    canonical_id_by_incoming_id = {"cap_x": "cap_x"}

    validate_classification_edge_endpoints(graph, (edge,), canonical_id_by_incoming_id)

    assert len(graph.calls) == 1
    assert graph.calls[0].params == {"ids": ["pa_secure_sdlc_4a7c1d"]}


def test_unresolved_endpoint_missing_everywhere_raises_before_any_write() -> None:
    """AC-BI-008: an endpoint absent from BOTH `canonical_id_by_incoming_id`
    AND `single_tenant_graph` raises `CompanyMergePersistenceError` -- the
    one call made is the read itself, never a write.
    """
    graph = _FakeGraph(existing_ids=frozenset())
    edge = BareEdge(
        relationship_type="VERIFIED_BY", source_id="rp_never_persisted", target_id="ctrl_x"
    )

    with pytest.raises(CompanyMergePersistenceError, match="rp_never_persisted"):
        validate_classification_edge_endpoints(graph, (edge,), {})

    assert len(graph.calls) == 1


def test_only_the_missing_endpoint_is_named_in_the_error() -> None:
    """A partially-resolved batch (one endpoint found, one missing) raises
    naming only the missing one -- proves the check is per-id, not
    all-or-nothing.
    """
    graph = _FakeGraph(existing_ids=frozenset({"pa_found"}))
    edges = (
        BareEdge(relationship_type="COVERS", source_id="pa_found", target_id="cap_x"),
        BareEdge(relationship_type="MITIGATED_BY", source_id="rp_missing", target_id="cap_x"),
    )
    canonical_id_by_incoming_id = {"cap_x": "cap_x"}

    with pytest.raises(CompanyMergePersistenceError, match="rp_missing") as exc_info:
        validate_classification_edge_endpoints(graph, edges, canonical_id_by_incoming_id)

    assert "pa_found" not in str(exc_info.value)


def _minimal_baseline_graph(**overrides: object) -> BaselineGraph:
    defaults: dict[str, object] = {
        "regulatory_instrument_id": "REG-1.0",
        "regulatory_instrument_properties": {},
        "role_nodes": (),
        "requirement_nodes": (),
        "obligation_nodes": (),
        "capability_nodes": (),
        "provenance_edges": (),
        "bare_edges": (),
    }
    defaults.update(overrides)
    return BaselineGraph(**defaults)  # type: ignore[arg-type]


def test_classification_write_counts_reports_every_key_correctly() -> None:
    """AC-BI-011: all six counts computed correctly from a baseline carrying
    a known, DIFFERENT count of each -- so a bug that swaps two counts (e.g.
    `owns_count`/`covers_count`) cannot pass by coincidence.
    """
    graph = _minimal_baseline_graph(
        practice_area_nodes=(_pa("pa_1"), _pa("pa_2")),
        risk_path_nodes=(_rp("rp_1"),),
        classification_edges=(
            BareEdge(relationship_type="COVERS", source_id="pa_1", target_id="cap_1"),
            BareEdge(relationship_type="COVERS", source_id="pa_1", target_id="cap_2"),
            BareEdge(relationship_type="COVERS", source_id="pa_2", target_id="cap_3"),
            BareEdge(relationship_type="OWNS", source_id="pa_1", target_id="pol_1"),
            BareEdge(relationship_type="MITIGATED_BY", source_id="rp_1", target_id="cap_1"),
            BareEdge(relationship_type="VERIFIED_BY", source_id="rp_1", target_id="ctrl_1"),
            BareEdge(relationship_type="VERIFIED_BY", source_id="rp_1", target_id="ctrl_2"),
        ),
    )

    assert classification_write_counts(graph) == {
        "practice_area_count": 2,
        "risk_path_count": 1,
        "covers_count": 3,
        "owns_count": 1,
        "mitigated_by_count": 1,
        "verified_by_count": 2,
    }


def test_classification_write_counts_all_zero_for_external_baseline() -> None:
    """A baseline with no classification-layer content at all reports every
    count as zero, never raising or omitting a key.
    """
    graph = _minimal_baseline_graph()

    assert classification_write_counts(graph) == {
        "practice_area_count": 0,
        "risk_path_count": 0,
        "covers_count": 0,
        "owns_count": 0,
        "mitigated_by_count": 0,
        "verified_by_count": 0,
    }


def _pa(node_id: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": node_id, "status": "active"})


def _rp(node_id: str) -> BaselineNode:
    return BaselineNode(id=node_id, properties={"name": node_id, "status": "active"})
