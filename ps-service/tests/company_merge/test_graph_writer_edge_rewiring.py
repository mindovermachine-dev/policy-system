"""Tests for `ps_service.company_merge.graph_writer.persist_rewired_edges`
(PLAN_REVIEWED.md §10 Increment 12, §6.2): the edge writer that mirrors
`domain_mapper.graph_writer._upsert_bare_edge`'s `MATCH ... MERGE` shape,
with the Obligation-typed endpoint of each `HAS`/`SATISFIED_BY`/`REQUIRES`
edge rewritten through the caller-supplied canonical-id mapping instead of
written as its baseline-local id.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ps_service.company_merge.errors import CompanyMergePersistenceError
from ps_service.company_merge.graph_writer import persist_rewired_edges
from ps_service.company_merge.models import BareEdge


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
    """Satisfies `GraphHandle` structurally, capturing every `(query,
    params)` call for assertion."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


def test_has_edge_target_is_rewritten_to_canonical_id() -> None:
    """A HAS edge with a resolution mapping the baseline-local Obligation id
    onto a DIFFERENT existing node's canonical id -- the written edge's
    target must be the canonical id, never the baseline-local one."""
    graph = _FakeGraph()
    edge = BareEdge(
        relationship_type="HAS", source_id="role_manufacturer_abc123", target_id="obl_baseline_local"
    )
    canonical_id_by_incoming_id = {"obl_baseline_local": "obl_canonical_existing"}

    persist_rewired_edges(graph, (edge,), canonical_id_by_incoming_id)

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == (
        "MATCH (s:Role {id: $source_id}), (t:Obligation {id: $target_id}) "
        "MERGE (s)-[:HAS]->(t)"
    )
    # Exact dict equality already proves the target is the canonical id,
    # never the baseline-local one.
    assert call.params == {
        "source_id": "role_manufacturer_abc123",
        "target_id": "obl_canonical_existing",
    }


def test_satisfied_by_edge_target_is_rewritten_to_canonical_id() -> None:
    graph = _FakeGraph()
    edge = BareEdge(
        relationship_type="SATISFIED_BY",
        source_id="CRA-1.0_req_art_13.1",
        target_id="obl_baseline_local",
    )
    canonical_id_by_incoming_id = {"obl_baseline_local": "obl_canonical_existing"}

    persist_rewired_edges(graph, (edge,), canonical_id_by_incoming_id)

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == (
        "MATCH (s:Requirement {id: $source_id}), (t:Obligation {id: $target_id}) "
        "MERGE (s)-[:SATISFIED_BY]->(t)"
    )
    assert call.params == {
        "source_id": "CRA-1.0_req_art_13.1",
        "target_id": "obl_canonical_existing",
    }


def test_requires_edge_source_is_rewritten_to_canonical_id() -> None:
    """A REQUIRES edge is rewritten on its SOURCE id (Obligation is the
    source of REQUIRES), not its target. Its Capability target
    (`cap_encrypt_data`) is a realistic exact-match resolution -- mapping
    onto itself -- since validation now requires an entry for every
    dedupe-eligible endpoint (Obligation OR Capability), not just the
    Obligation-typed one."""
    graph = _FakeGraph()
    edge = BareEdge(
        relationship_type="REQUIRES", source_id="obl_baseline_local", target_id="cap_encrypt_data"
    )
    canonical_id_by_incoming_id = {
        "obl_baseline_local": "obl_canonical_existing",
        "cap_encrypt_data": "cap_encrypt_data",
    }

    persist_rewired_edges(graph, (edge,), canonical_id_by_incoming_id)

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == (
        "MATCH (s:Obligation {id: $source_id}), (t:Capability {id: $target_id}) "
        "MERGE (s)-[:REQUIRES]->(t)"
    )
    # Exact dict equality already proves the source is the canonical id,
    # never the baseline-local one.
    assert call.params == {
        "source_id": "obl_canonical_existing",
        "target_id": "cap_encrypt_data",
    }


def test_requires_edge_target_is_rewritten_to_canonical_capability_id() -> None:
    """The bug this test guards against: a REQUIRES edge's Capability target
    that is exact-or-semantically matched onto a DIFFERENT existing
    canonical Capability id (its baseline-local id maps to a different
    canonical id in canonical_id_by_incoming_id) must be written with the
    CANONICAL Capability id as its target, not the baseline-local one. Prior
    to the fix, only the Obligation-typed (source) endpoint of REQUIRES was
    ever rewritten -- the Capability target passed through unchanged
    unconditionally, so a matched (never separately node-written) Capability
    would make the MATCH clause match zero rows and the MERGE would silently
    write NOTHING at all. So this test asserts both that a call WAS made
    (the previously-missing-edge case) and that its target is correct."""
    graph = _FakeGraph()
    edge = BareEdge(
        relationship_type="REQUIRES",
        source_id="obl_baseline_local",
        target_id="cap_baseline_local",
    )
    canonical_id_by_incoming_id = {
        "obl_baseline_local": "obl_canonical_existing",
        "cap_baseline_local": "cap_canonical_existing",
    }

    persist_rewired_edges(graph, (edge,), canonical_id_by_incoming_id)

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == (
        "MATCH (s:Obligation {id: $source_id}), (t:Capability {id: $target_id}) "
        "MERGE (s)-[:REQUIRES]->(t)"
    )
    assert call.params == {
        "source_id": "obl_canonical_existing",
        "target_id": "cap_canonical_existing",
    }


def test_role_and_requirement_endpoints_pass_through_unchanged() -> None:
    """Role (HAS's source) and Requirement (SATISFIED_BY's source) are
    never in any resolution mapping -- they pass through unchanged, and an
    empty mapping doesn't break either of them."""
    graph = _FakeGraph()
    has_edge = BareEdge(
        relationship_type="HAS", source_id="role_manufacturer_abc123", target_id="obl_x"
    )
    satisfied_by_edge = BareEdge(
        relationship_type="SATISFIED_BY", source_id="CRA-1.0_req_art_13.1", target_id="obl_x"
    )
    canonical_id_by_incoming_id = {"obl_x": "obl_x"}  # exact match: canonical id == incoming id

    persist_rewired_edges(graph, (has_edge, satisfied_by_edge), canonical_id_by_incoming_id)

    assert len(graph.calls) == 2
    assert graph.calls[0].params == {
        "source_id": "role_manufacturer_abc123",
        "target_id": "obl_x",
    }
    assert graph.calls[1].params == {
        "source_id": "CRA-1.0_req_art_13.1",
        "target_id": "obl_x",
    }


def test_rerunning_identical_edge_set_targets_the_same_triple_both_times() -> None:
    """Idempotency shape check: re-running the identical edge set twice
    against a fake graph that tracks distinct (source, relationship_type,
    target) triples -- the second run's MERGE targets the exact same
    triple as the first (full duplicate-count proof against real FalkorDB
    MERGE semantics is deferred to the live capstone, PLAN_REVIEWED.md §10
    Increment 12)."""
    graph = _FakeGraph()
    edges = (
        BareEdge(relationship_type="HAS", source_id="role_manufacturer_abc123", target_id="obl_x"),
        BareEdge(relationship_type="REQUIRES", source_id="obl_x", target_id="cap_encrypt_data"),
    )
    canonical_id_by_incoming_id = {
        "obl_x": "obl_canonical_x",
        "cap_encrypt_data": "cap_encrypt_data",
    }

    for _ in range(2):
        persist_rewired_edges(graph, edges, canonical_id_by_incoming_id)

    assert len(graph.calls) == 4

    def _triple(call: _RecordedCall) -> tuple[object, str, object]:
        assert call.params is not None
        return (call.params["source_id"], call.query, call.params["target_id"])

    first_run_triples = {_triple(call) for call in graph.calls[:2]}
    second_run_triples = {_triple(call) for call in graph.calls[2:]}
    assert first_run_triples == second_run_triples
    assert first_run_triples == {
        ("role_manufacturer_abc123", graph.calls[0].query, "obl_canonical_x"),
        ("obl_canonical_x", graph.calls[1].query, "cap_encrypt_data"),
    }


def test_missing_canonical_mapping_raises_before_any_write() -> None:
    """An edge whose Obligation-typed endpoint has no entry in
    canonical_id_by_incoming_id raises CompanyMergePersistenceError, with
    zero graph.query calls made -- validate-then-write over the whole
    collection, mirroring domain_mapper.graph_writer's own B3 fix shape."""
    graph = _FakeGraph()
    edges = (
        BareEdge(relationship_type="HAS", source_id="role_manufacturer_abc123", target_id="obl_x"),
        BareEdge(relationship_type="HAS", source_id="role_importer_def456", target_id="obl_unmapped"),
    )
    canonical_id_by_incoming_id = {"obl_x": "obl_canonical_x"}

    with pytest.raises(CompanyMergePersistenceError, match="obl_unmapped"):
        persist_rewired_edges(graph, edges, canonical_id_by_incoming_id)

    assert graph.calls == []


def test_requires_edge_target_missing_from_mapping_raises_before_any_write() -> None:
    """A REQUIRES edge whose Capability-typed TARGET has no entry in
    canonical_id_by_incoming_id raises CompanyMergePersistenceError, with
    zero graph.query calls made. This closes the gap left by the prior fix:
    validation now requires an entry for EVERY dedupe-eligible endpoint
    (Obligation or Capability), not just the one endpoint (REQUIRES's
    Obligation-typed source) guaranteed present by a single edge type's own
    construction. Absent this check, a REQUIRES edge with an incomplete
    caller-supplied mapping would silently write its baseline-local
    Capability id verbatim instead of raising -- reproducing a milder
    version of the exact dangling-edge bug the write-side fix already
    closed, just triggered by an incomplete mapping instead of a
    never-looked-up endpoint."""
    graph = _FakeGraph()
    edge = BareEdge(
        relationship_type="REQUIRES",
        source_id="obl_baseline_local",
        target_id="cap_unmapped",
    )
    canonical_id_by_incoming_id = {"obl_baseline_local": "obl_canonical_existing"}

    with pytest.raises(CompanyMergePersistenceError, match="cap_unmapped"):
        persist_rewired_edges(graph, (edge,), canonical_id_by_incoming_id)

    assert graph.calls == []
