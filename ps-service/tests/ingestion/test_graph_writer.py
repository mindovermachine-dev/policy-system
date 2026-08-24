"""Tests for `ps_service.ingestion.graph_writer` (PLAN_REVIEWED.md §7
Increments 8-10).

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.ingestion.falkordb_client`) structurally — no mocking
library, matching L2 Testing Patterns' "mock at component boundaries."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import pytest
import redis.exceptions
from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.ingestion.errors import IngestionPersistenceError
from ps_service.ingestion.falkordb_client import FalkorDB, connect, select_graph
from ps_service.ingestion.graph_writer import (
    persist_native_structural_graph,
    register_regulation_version,
    verify_structural_graph_reachable,
)
from ps_service.ingestion.models import (
    ReachabilityCount,
    RegulationMetadata,
    StructuralEdge,
    StructuralNode,
)


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
    params)` call for assertion — this is what lets the B1 regression tests
    below assert `graph.calls == []` (zero writes) rather than merely
    "raised eventually"."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(_RecordedCall(q, params))
        return _FakeQueryResult([[0]])


_LABEL_RE = re.compile(r"n:(\w+)\)")


class _ScriptedReachabilityGraph:
    """Fake `GraphHandle` for `verify_structural_graph_reachable` tests:
    returns a scripted `(total, reachable)` pair per label, determined by
    parsing which label the query string names — deliberately independent
    of `_KNOWN_ELEMENT_TYPES` frozenset iteration order (which varies
    process-to-process under randomized `str` hashing/`PYTHONHASHSEED`, so
    a test must not assume a fixed call order over that set)."""

    def __init__(self, counts_by_label: dict[str, tuple[int, int]]) -> None:
        self._counts_by_label = counts_by_label
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        match = _LABEL_RE.search(q)
        assert match is not None, f"could not find a label in query: {q!r}"
        total, reachable = self._counts_by_label[match.group(1)]
        value = reachable if "DISTINCT" in q else total
        return _FakeQueryResult([[value]])


def _metadata() -> RegulationMetadata:
    return RegulationMetadata(
        title="Cyber Resilience Act",
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external",
    )


def _node(element_type: str, node_id: str) -> StructuralNode:
    return StructuralNode(
        element_type, node_id, {"text": "text", "citation_ref": "Art. 1", "order": 1}
    )


def _edge(
    parent_element_type: str, parent_id: str, child_element_type: str, child_id: str
) -> StructuralEdge:
    return StructuralEdge(parent_element_type, parent_id, child_element_type, child_id)


# --- Increment 8: register_regulation_version --------------------------


def test_register_regulation_version_merges_regulation_node_with_parameterized_metadata() -> None:
    graph = _FakeGraph()

    register_regulation_version(graph, "CRA-1.0", _metadata())

    assert len(graph.calls) == 1
    call = graph.calls[0]
    assert call.query == "MERGE (n:Regulation {id: $id}) SET n += $properties"
    assert call.params == {
        "id": "CRA-1.0",
        "properties": {
            "title": "Cyber Resilience Act",
            "jurisdiction": "EU",
            "effective_date": "2027-12-11",
            "version": "1.0",
            "status": "active",
            "source_type": "external",
        },
    }


def test_register_regulation_version_never_interpolates_metadata_into_query_string() -> None:
    """The injection-safety property: every metadata value (and the
    regulation id) flows through `params` only — none of them ever appear
    as a substring of the query string itself."""
    graph = _FakeGraph()

    register_regulation_version(graph, "CRA-1.0", _metadata())

    query = graph.calls[0].query
    for value in (
        "Cyber Resilience Act",
        "EU",
        "2027-12-11",
        "1.0",
        "active",
        "external",
        "CRA-1.0",
    ):
        assert value not in query


# --- Increment 9: persist_native_structural_graph (THE B1 FIX) ----------


def test_persist_native_structural_graph_writes_every_valid_node_and_edge() -> None:
    graph = _FakeGraph()
    nodes = (
        _node("ARTICLE", "CRA#art_1"),
        _node("PARAGRAPH", "CRA#art_1.001"),
    )
    edges = (
        _edge("Regulation", "CRA-1.0", "ARTICLE", "CRA#art_1"),
        _edge("ARTICLE", "CRA#art_1", "PARAGRAPH", "CRA#art_1.001"),
    )

    persist_native_structural_graph(graph, "CRA-1.0", nodes, edges)

    assert len(graph.calls) == 4
    first_node_call, second_node_call, first_edge_call, second_edge_call = graph.calls

    assert first_node_call.query == "MERGE (n:ARTICLE {id: $id}) SET n += $properties"
    assert first_node_call.params == {"id": "CRA#art_1", "properties": nodes[0].properties}
    assert second_node_call.query == "MERGE (n:PARAGRAPH {id: $id}) SET n += $properties"
    assert second_node_call.params == {
        "id": "CRA#art_1.001",
        "properties": nodes[1].properties,
    }

    assert first_edge_call.query == (
        "MATCH (a:Regulation {id: $parent_id}), (b:ARTICLE {id: $child_id}) "
        "MERGE (a)-[:HAS]->(b)"
    )
    assert first_edge_call.params == {"parent_id": "CRA-1.0", "child_id": "CRA#art_1"}
    assert second_edge_call.query == (
        "MATCH (a:ARTICLE {id: $parent_id}), (b:PARAGRAPH {id: $child_id}) "
        "MERGE (a)-[:HAS]->(b)"
    )
    assert second_edge_call.params == {
        "parent_id": "CRA#art_1",
        "child_id": "CRA#art_1.001",
    }


def test_persist_native_structural_graph_raises_before_any_write_when_first_node_invalid() -> None:
    graph = _FakeGraph()
    nodes = (_node("BOGUS", "CRA#bogus_1"),)

    with pytest.raises(IngestionPersistenceError):
        persist_native_structural_graph(graph, "CRA-1.0", nodes, ())

    assert graph.calls == []


def test_persist_native_structural_graph_raises_with_zero_writes_when_invalid_node_follows_valid_one() -> (
    None
):
    """The critical B1 regression test.

    Against the ORIGINAL per-element-interleaved design (allow-list check
    performed inside `_upsert_node`, called from a loop that interleaves
    validate-then-write per node), the first (valid) node here would have
    already been written via a real `graph.query` MERGE call *before* the
    loop ever reached the second (invalid) node — so at the point of the
    raise, the fake graph's `calls` list would contain 1 entry, not 0.
    `assert graph.calls == []` below is exactly the assertion that design
    would have failed: it checks zero calls total, not merely "some calls
    happened before the raise."

    The B1 fix (`_validate_element_types` as a whole-collection first
    pass, run to completion before any `_upsert_node`/`_upsert_edge` call)
    makes this pass, because both nodes are validated before either one is
    written.
    """
    graph = _FakeGraph()
    nodes = (
        _node("ARTICLE", "CRA#art_1"),  # valid, listed first
        _node("BOGUS", "CRA#bogus_1"),  # invalid, listed second
    )

    with pytest.raises(IngestionPersistenceError):
        persist_native_structural_graph(graph, "CRA-1.0", nodes, ())

    assert graph.calls == []


def test_persist_native_structural_graph_raises_with_zero_writes_when_invalid_edge_follows_valid_ones() -> (
    None
):
    """Edge half of the B1 regression proof (mirrors the node case above).

    One valid node and one valid edge are listed first, followed by a
    second edge with an invalid `child_element_type`. Under the original
    interleaved design, the valid node and the valid first edge would both
    already be written (2 calls) before the loop reached the invalid
    second edge. `assert graph.calls == []` is the same zero-calls-total
    proof as the node case.
    """
    graph = _FakeGraph()
    nodes = (_node("ARTICLE", "CRA#art_1"),)
    edges = (
        _edge("Regulation", "CRA-1.0", "ARTICLE", "CRA#art_1"),  # valid, listed first
        _edge("ARTICLE", "CRA#art_1", "BOGUS", "CRA#bogus_1"),  # invalid, listed second
    )

    with pytest.raises(IngestionPersistenceError):
        persist_native_structural_graph(graph, "CRA-1.0", nodes, edges)

    assert graph.calls == []


def test_persist_native_structural_graph_substitutes_real_regulation_id_for_celex_placeholder_parent_id() -> (
    None
):
    """Increment 13 regression test — the bug this increment found and
    fixed (see `graph_writer.py`'s module docstring, "Increment 13 fix").

    The real Cellar/ELI adapter never sees the final `{SHORT}-{VERSION}`
    regulation id — `parse_structure` stamps every Regulation-anchored
    edge's `parent_id` with the raw CELEX identifier it was given instead
    (e.g. `"32024R2847"`), a placeholder. Every other test in this file
    accidentally masks this: their own `_edge("Regulation", ...)` fixtures
    set `parent_id` equal to the `regulation_id` passed to
    `persist_native_structural_graph` by construction, which never happens
    for real adapter output. This test deliberately uses a *different*
    `parent_id` (mimicking the real CELEX-vs-final-id mismatch) and asserts
    the write still targets the real `regulation_id` — not the
    placeholder — which is what makes the Regulation node's `MATCH`
    actually find a row and the `MERGE` actually fire.
    """
    graph = _FakeGraph()
    nodes = (_node("ANNEX", "32024R2847#anx_I"),)
    edges = (_edge("Regulation", "32024R2847", "ANNEX", "32024R2847#anx_I"),)

    persist_native_structural_graph(graph, "CRA-1.0", nodes, edges)

    edge_call = graph.calls[-1]
    assert edge_call.query == (
        "MATCH (a:Regulation {id: $parent_id}), (b:ANNEX {id: $child_id}) MERGE (a)-[:HAS]->(b)"
    )
    assert edge_call.params == {"parent_id": "CRA-1.0", "child_id": "32024R2847#anx_I"}


def test_persist_native_structural_graph_raises_on_invalid_parent_element_type() -> None:
    """Bonus coverage: the parent-side allow-list check (the one branch of
    `_validate_element_types` not otherwise exercised above — every other
    test's valid edges use `"Regulation"` as `parent_element_type`)."""
    graph = _FakeGraph()
    nodes = (_node("ARTICLE", "CRA#art_1"),)
    edges = (_edge("BOGUS", "CRA#bogus_1", "ARTICLE", "CRA#art_1"),)

    with pytest.raises(IngestionPersistenceError):
        persist_native_structural_graph(graph, "CRA-1.0", nodes, edges)

    assert graph.calls == []


# --- Increment 10: verify_structural_graph_reachable ---------------------

_ALL_LABELS_NO_GAP: dict[str, tuple[int, int]] = {
    "Regulation": (1, 1),
    "TITLE": (0, 0),
    "CHAPTER": (5, 5),
    "SECTION": (3, 3),
    "ARTICLE": (71, 71),
    "PARAGRAPH": (140, 140),
    "ANNEX": (8, 8),
    "RECITAL": (130, 130),
}


def test_verify_structural_graph_reachable_returns_counts_when_no_gap() -> None:
    graph = _ScriptedReachabilityGraph(_ALL_LABELS_NO_GAP)

    result = verify_structural_graph_reachable(graph, "CRA-1.0")

    assert result == {
        label: ReachabilityCount(total=total, reachable=reachable)
        for label, (total, reachable) in _ALL_LABELS_NO_GAP.items()
    }


def test_verify_structural_graph_reachable_raises_on_gap() -> None:
    counts_with_gap = dict(_ALL_LABELS_NO_GAP)
    counts_with_gap["ARTICLE"] = (71, 70)  # one article not reachable from Regulation
    graph = _ScriptedReachabilityGraph(counts_with_gap)

    with pytest.raises(IngestionPersistenceError, match="ARTICLE"):
        verify_structural_graph_reachable(graph, "CRA-1.0")


# --- Dependency health wiring ----------------------------------------------


class _FakeGraphThatRaisesConnectionError:
    """Satisfies `GraphHandle` structurally; every `query()` call raises
    `redis.exceptions.ConnectionError` — the same exception shape a real
    unreachable FalkorDB instance raises mid-write."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        raise redis.exceptions.ConnectionError("Error 111 connecting to 127.0.0.1:6379")


def test_register_regulation_version_marks_falkordb_unhealthy_on_connection_error() -> None:
    graph = _FakeGraphThatRaisesConnectionError()

    with pytest.raises(IngestionPersistenceError):
        register_regulation_version(graph, "CRA-1.0", _metadata())

    assert is_healthy(FALKORDB) is False


def test_register_regulation_version_marks_falkordb_healthy_on_success() -> None:
    graph = _FakeGraph()

    register_regulation_version(graph, "CRA-1.0", _metadata())

    assert is_healthy(FALKORDB) is True


def test_falkordb_self_heals_after_a_later_successful_write() -> None:
    failing_graph = _FakeGraphThatRaisesConnectionError()
    with pytest.raises(IngestionPersistenceError):
        register_regulation_version(failing_graph, "CRA-1.0", _metadata())
    assert is_healthy(FALKORDB) is False

    healthy_graph = _FakeGraph()
    register_regulation_version(healthy_graph, "CRA-1.0", _metadata())

    assert is_healthy(FALKORDB) is True


def test_data_validation_error_does_not_mark_falkordb_unhealthy() -> None:
    """A B1-style validation failure (bad `element_type`) never reaches
    `graph.query()` at all — it must not be mistaken for a FalkorDB outage."""
    graph = _FakeGraph()
    nodes = (_node("NOT_A_REAL_TYPE", "CRA#bad"),)

    with pytest.raises(IngestionPersistenceError, match="element_type"):
        persist_native_structural_graph(graph, "CRA-1.0", nodes, ())

    assert is_healthy(FALKORDB) is True


# --- Live FalkorDB wiring proof -------------------------------------------

_LIVE_TEST_GRAPH_NAME = "graph_writer_live_test_native"


def _delete_graph_if_exists(db: FalkorDB, name: str) -> None:
    if name in db.list_graphs():
        db.select_graph(name).delete()


@pytest.mark.falkordb_live
def test_graph_writer_functions_work_against_real_falkordb() -> None:
    """Live wiring proof, real FalkorDB at 127.0.0.1:6379.

    The fakes above prove `_validate_element_types`'s B1 fix and the exact
    Cypher shape at the unit level, but only a real round-trip confirms the
    actual query syntax is valid Cypher and behaves as those unit tests
    assume: `MERGE ... SET n += $properties` with a nested dict param,
    `MERGE`/`MATCH` label interpolation for both nodes and edges, and the
    `-[:HAS*1..]->` variable-length traversal `verify_structural_graph_
    reachable` depends on. Uses a dedicated, test-only graph name, deleted
    before and after so the run is idempotent and leaves no residue.
    """
    db = connect(host="127.0.0.1", port=6379)
    _delete_graph_if_exists(db, _LIVE_TEST_GRAPH_NAME)
    graph = select_graph(db, _LIVE_TEST_GRAPH_NAME)

    try:
        register_regulation_version(graph, "GWTEST-1.0", _metadata())

        nodes = (_node("ARTICLE", "GWTEST#art_1"),)
        edges = (_edge("Regulation", "GWTEST-1.0", "ARTICLE", "GWTEST#art_1"),)
        persist_native_structural_graph(graph, "GWTEST-1.0", nodes, edges)

        counts = verify_structural_graph_reachable(graph, "GWTEST-1.0")

        assert counts["Regulation"] == ReachabilityCount(total=1, reachable=1)
        assert counts["ARTICLE"] == ReachabilityCount(total=1, reachable=1)
        for label in ("TITLE", "CHAPTER", "SECTION", "PARAGRAPH", "ANNEX", "RECITAL"):
            assert counts[label] == ReachabilityCount(total=0, reachable=0)

        regulation_row = graph.query(
            "MATCH (n:Regulation {id: $id}) RETURN n.title, n.effective_date",
            params={"id": "GWTEST-1.0"},
        ).result_set[0]
        assert regulation_row == ["Cyber Resilience Act", "2027-12-11"]
    finally:
        _delete_graph_if_exists(db, _LIVE_TEST_GRAPH_NAME)
