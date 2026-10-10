"""Tests for `ps_service.ingestion.graph_writer` (PLAN_REVIEWED.md §7
Increments 8-10).

Fakes implement the `GraphHandle`/`GraphQueryResult` Protocols
(`ps_service.ingestion.falkordb_client`) structurally — no mocking
library, matching L2 Testing Patterns' "mock at component boundaries."
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from graph_gateway.live_endpoints import falkordb_endpoint
from graph_gateway.live_postgres import committed_audit_event

from ps_service.dependency_health import FALKORDB, is_healthy
from ps_service.graph_gateway.gateway import GraphWriteGateway
from ps_service.graph_gateway.store import PsycopgGraphLogStore
from ps_service.ingestion.errors import IngestionPersistenceError
from ps_service.ingestion.falkordb_client import FalkorDB, connect, select_graph
from ps_service.ingestion.graph_writer import (
    build_native_group,
    build_native_writes,
    read_existing_nodes,
    verify_structural_graph_reachable,
)
from ps_service.ingestion.models import (
    InstrumentType,
    ReachabilityCount,
    RegulatoryInstrumentMetadata,
    RegulatoryInstrumentStatus,
    SourceType,
    StructuralEdge,
    StructuralNode,
)

if TYPE_CHECKING:
    from persistence.provisioned_postgres import Provisioned


class _FakeQueryResult:
    """Satisfies `GraphQueryResult` structurally."""

    def __init__(self, result_set: list[object]) -> None:
        self._result_set = result_set

    @property
    def result_set(self) -> list[object]:
        return self._result_set


_LABEL_RE = re.compile(r"n:(\w+)\)")


class _ScriptedReachabilityGraph:
    """Fake `GraphHandle` for `verify_structural_graph_reachable` tests:
    returns a scripted `(total, reachable)` pair per label, determined by
    parsing which label the query string names — deliberately independent
    of `_KNOWN_ELEMENT_TYPES` frozenset iteration order (which varies
    process-to-process under randomized `str` hashing/`PYTHONHASHSEED`, so
    a test must not assume a fixed call order over that set).
    """

    def __init__(self, counts_by_label: dict[str, tuple[int, int]]) -> None:
        self._counts_by_label = counts_by_label
        self.calls: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        self.calls.append(q)
        match = _LABEL_RE.search(q)
        assert match is not None, f"could not find a label in query: {q!r}"
        total, reachable = self._counts_by_label[match.group(1)]
        # `verify` issues two query shapes per label: the bare total
        # (`count(n)`) and the orphan count (`... WHERE NOT (:RegulatoryInstrument)
        # -[:HAS*1..]->(n) ...`), which the scripted (total, reachable) pair
        # models as `total - reachable`.
        value = total - reachable if "WHERE NOT" in q else total
        return _FakeQueryResult([[value]])


def _metadata(
    *,
    title: str = "Cyber Resilience Act",
    jurisdiction: str = "EU",
    effective_date: date = date(2027, 12, 11),
    version: str = "1.0",
    status: RegulatoryInstrumentStatus = "active",
    source_type: SourceType = "external",
    instrument_type: InstrumentType | None = "regulation",
    celex: str | None = None,
) -> RegulatoryInstrumentMetadata:
    return RegulatoryInstrumentMetadata(
        title=title,
        jurisdiction=jurisdiction,
        effective_date=effective_date,
        version=version,
        status=status,
        source_type=source_type,
        instrument_type=instrument_type,
        celex=celex,
    )


def _node(element_type: str, node_id: str) -> StructuralNode:
    return StructuralNode(
        element_type, node_id, {"text": "text", "citation_ref": "Art. 1", "order": 1}
    )


# --- Increment 10: verify_structural_graph_reachable ---------------------

_ALL_LABELS_NO_GAP: dict[str, tuple[int, int]] = {
    "RegulatoryInstrument": (1, 1),
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


def test_verify_passes_with_two_versions_coexisting() -> None:
    """Follow-on A: after a Change-Monitor re-ingest, one `{short}_native`
    graph holds two versions' subtrees. RECITAL's whole-graph total is v1's
    173, every one still reachable from v1 (0 orphans), while the id under
    verification is v2 — whose own document has no preamble. The orphan
    check must NOT read v2's zero recitals as a persistence gap.
    """
    counts = dict(_ALL_LABELS_NO_GAP)
    counts["RECITAL"] = (173, 173)  # 173 total, all reachable from some RI -> 0 orphans
    graph = _ScriptedReachabilityGraph(counts)

    result = verify_structural_graph_reachable(graph, "CRA-2.0")

    assert result["RECITAL"] == ReachabilityCount(total=173, reachable=173)


def test_verify_raises_on_true_orphan() -> None:
    """A node that `persist` wrote but left unlinked from every
    RegulatoryInstrument is a genuine orphan and must still raise.
    """
    counts = dict(_ALL_LABELS_NO_GAP)
    counts["ARTICLE"] = (100, 98)  # 2 of 100 reachable from no RI at all
    graph = _ScriptedReachabilityGraph(counts)

    with pytest.raises(IngestionPersistenceError, match="ARTICLE"):
        verify_structural_graph_reachable(graph, "CRA-2.0")


# --- Dependency health wiring ----------------------------------------------


class _FakeGraphThatRaisesConnectionError:
    """Satisfies `GraphHandle` structurally; every `query()` call raises
    `redis.exceptions.ConnectionError` — the same exception shape a real
    unreachable FalkorDB instance raises mid-write.
    """

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        raise redis.exceptions.ConnectionError("Error 111 connecting to 127.0.0.1:6379")


def test_a_connection_error_on_a_read_marks_falkordb_unhealthy() -> None:
    graph = _FakeGraphThatRaisesConnectionError()

    with pytest.raises(IngestionPersistenceError):
        verify_structural_graph_reachable(graph, "CRA-1.0")

    assert is_healthy(FALKORDB) is False


def test_a_successful_read_marks_falkordb_healthy() -> None:
    verify_structural_graph_reachable(_ScriptedReachabilityGraph(_ALL_LABELS_NO_GAP), "CRA-1.0")

    assert is_healthy(FALKORDB) is True


def test_falkordb_self_heals_after_a_later_successful_read() -> None:
    with pytest.raises(IngestionPersistenceError):
        verify_structural_graph_reachable(_FakeGraphThatRaisesConnectionError(), "CRA-1.0")
    assert is_healthy(FALKORDB) is False

    verify_structural_graph_reachable(_ScriptedReachabilityGraph(_ALL_LABELS_NO_GAP), "CRA-1.0")

    assert is_healthy(FALKORDB) is True


def test_data_validation_error_does_not_mark_falkordb_unhealthy() -> None:
    """A B1-style validation failure (bad `element_type`) never reaches the graph at all —
    it must not be mistaken for a FalkorDB outage.
    """
    nodes = (_node("NOT_A_REAL_TYPE", "CRA#bad"),)

    with pytest.raises(IngestionPersistenceError, match="element_type"):
        build_native_writes("CRA-1.0", _metadata(), nodes, ())

    assert is_healthy(FALKORDB) is True


# --- Live FalkorDB wiring proof -------------------------------------------

_LIVE_TEST_GRAPH_NAME = "graph_writer_live_test_native"


def _delete_graph_if_exists(db: FalkorDB, name: str) -> None:
    if name in db.list_graphs():
        db.select_graph(name).delete()


@pytest.mark.falkordb_live
@pytest.mark.postgres_live
def test_graph_writer_functions_work_against_real_falkordb(
    provisioned_graph_log: Provisioned,
) -> None:
    """Live wiring proof: the native graph written through the real gateway, read by the writer.

    The fakes above prove `_validate_element_types`'s B1 fix and the primitives at the unit level,
    but only a real round-trip confirms that the group the writer builds is accepted by the
    gateway's Cypher on real FalkorDB, and that `verify_structural_graph_reachable`'s
    `-[:HAS*1..]->` traversal sees what the gateway wrote. Needs FalkorDB (host/port from
    `PS_TEST_FALKORDB_HOST`/`PS_TEST_FALKORDB_PORT`, default 127.0.0.1:6379) and a scratch
    Postgres for the log. Uses a dedicated, test-only graph name, deleted before and after.
    """
    host, port = falkordb_endpoint()
    db = connect(host=host, port=port)
    _delete_graph_if_exists(db, _LIVE_TEST_GRAPH_NAME)
    graph = select_graph(db, _LIVE_TEST_GRAPH_NAME)

    try:
        # The graph is written through the Graph Write Gateway (issue #208): the real log store
        # and the real FalkorDB opener, in place of the writer's former direct MERGE calls.
        gateway = GraphWriteGateway(
            log_store=PsycopgGraphLogStore(provisioned_graph_log.state_config()),
            graph_opener=lambda name: select_graph(db, name),
        )
        nodes = (_node("ARTICLE", "GWTEST#art_1"),)
        edges = (StructuralEdge("RegulatoryInstrument", "GWTEST-1.0", "ARTICLE", "GWTEST#art_1"),)
        writes = build_native_writes("GWTEST-1.0", _metadata(), nodes, edges)
        gateway.submit_group(
            build_native_group(
                writes,
                existing=read_existing_nodes(graph, writes),
                graph_name=_LIVE_TEST_GRAPH_NAME,
                audit_event_id=committed_audit_event(provisioned_graph_log),
            )
        )

        counts = verify_structural_graph_reachable(graph, "GWTEST-1.0")

        assert counts["RegulatoryInstrument"] == ReachabilityCount(total=1, reachable=1)
        assert counts["ARTICLE"] == ReachabilityCount(total=1, reachable=1)
        for label in ("TITLE", "CHAPTER", "SECTION", "PARAGRAPH", "ANNEX", "RECITAL"):
            assert counts[label] == ReachabilityCount(total=0, reachable=0)

        regulatory_instrument_row = graph.query(
            "MATCH (n:RegulatoryInstrument {id: $id}) "
            "RETURN n.title, n.effective_date, n.instrument_type",
            params={"id": "GWTEST-1.0"},
        ).result_set[0]
        assert regulatory_instrument_row == ["Cyber Resilience Act", "2027-12-11", "regulation"]
    finally:
        _delete_graph_if_exists(db, _LIVE_TEST_GRAPH_NAME)
