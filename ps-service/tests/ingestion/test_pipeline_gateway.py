"""The external ingestion pipeline writes its native graph through the gateway (#208, slice 6).

`ingest_regulatory_instrument` submits one native group (the RegulatoryInstrument, the structural
nodes, then the `HAS` edges) under the caller's audit event id, and only reads through the graph
handle it is given. Postgres and FalkorDB are the gateway rig's in-memory fakes; the read handle
is a scripted fake that records every query it receives.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from graph_gateway._fakes import Fault
from graph_gateway.checkpoint_checks import assert_head_checkpoints, assert_no_checkpoint

from ps_service.graph_gateway.digest import canonical_digest
from ps_service.ingestion.errors import IngestionPersistenceError
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
    StructuralEdge,
    StructuralNode,
)
from ps_service.ingestion.pipeline import ingest_regulatory_instrument

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from graph_gateway._fakes import GatewayRig

    from ps_service.logging import LogEmitter

_GRAPH = "cra_native"


class _Rows:
    def __init__(self, value: int) -> None:
        self.result_set: list[object] = [[value]]


class _ReadGraph:
    """A read handle answering every count with `0` (or the scripted orphans), recording queries."""

    def __init__(self, orphans_in: str | None = None) -> None:
        self.queries: list[str] = []
        self._orphans_in = orphans_in

    def query(self, q: str, params: dict[str, object] | None = None) -> _Rows:
        del params
        self.queries.append(q)
        orphaned = self._orphans_in is not None and f"(n:{self._orphans_in})" in q
        return _Rows(1 if orphaned and "WHERE NOT" in q else 0)


class _Adapter:
    def __init__(self, structure: FetchedRegulatoryInstrumentStructure) -> None:
        self._structure = structure

    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        del identifier
        return self._structure


def _structure(
    *, extra_edges: tuple[StructuralEdge, ...] = ()
) -> FetchedRegulatoryInstrumentStructure:
    metadata = RegulatoryInstrumentMetadata(
        title="Cyber Resilience Act",
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external",
        instrument_type="regulation",
        celex="32024R2847",
    )
    props: dict[str, str | int] = {"text": "t", "citation_ref": "Art. 1", "order": 1}
    return FetchedRegulatoryInstrumentStructure(
        metadata=metadata,
        nodes=(
            StructuralNode("ARTICLE", "32024R2847#art_1", props),
            StructuralNode("PARAGRAPH", "32024R2847#art_1.001", props),
        ),
        edges=(
            StructuralEdge("RegulatoryInstrument", "32024R2847", "ARTICLE", "32024R2847#art_1"),
            StructuralEdge("ARTICLE", "32024R2847#art_1", "PARAGRAPH", "32024R2847#art_1.001"),
            *extra_edges,
        ),
    )


@pytest.fixture
def emitter(make_emitter: Callable[..., tuple[LogEmitter, Path]]) -> LogEmitter:
    """The run-log emitter every ingestion here writes through (its file is not read)."""
    return make_emitter()[0]


class _Run:
    """One ingestion over a rig, logged under a fresh audit event id."""

    def __init__(self, rig: GatewayRig, emitter: LogEmitter) -> None:
        self.rig = rig
        self.read_graph = _ReadGraph()
        self.audit_event_id = str(uuid.uuid4())
        self.emitter = emitter
        rig.store.audit_rows.add(self.audit_event_id)

    def ingest(self, structure: FetchedRegulatoryInstrumentStructure | None = None) -> None:
        ingest_regulatory_instrument(
            "32024R2847",
            "CRA",
            version="1.0",
            adapter=_Adapter(structure or _structure()),
            graph=self.read_graph,
            submitter=self.rig.gateway,
            audit_event_id=self.audit_event_id,
            emitter=self.emitter,
        )


def test_the_native_graph_is_written_as_one_group_under_the_audit_event(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    run = _Run(rig, emitter)

    run.ingest()

    assert len(rig.store.groups) == 1
    group = rig.store.groups[0]
    assert group.graph == _GRAPH
    assert group.audit_event_id == run.audit_event_id
    graph = rig.graphs.open(_GRAPH)
    assert set(graph.nodes) == {
        ("RegulatoryInstrument", "CRA-1.0"),
        ("ARTICLE", "32024R2847#art_1"),
        ("PARAGRAPH", "32024R2847#art_1.001"),
    }
    assert {key[0:3] for key in graph.edges} == {
        ("HAS", "RegulatoryInstrument", "CRA-1.0"),
        ("HAS", "ARTICLE", "32024R2847#art_1"),
    }
    assert graph.nodes[("RegulatoryInstrument", "CRA-1.0")]["effective_date"] == "2027-12-11"


def test_the_graph_read_back_through_the_digest_is_stable_across_a_rerun(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    run = _Run(rig, emitter)
    run.ingest()
    first = canonical_digest(rig.graphs.open(_GRAPH))

    run.ingest()

    assert canonical_digest(rig.graphs.open(_GRAPH)) == first
    assert len(rig.store.groups) == 1  # the identical group changed nothing, so nothing was logged


def test_the_read_handle_only_ever_receives_reads(rig: GatewayRig, emitter: LogEmitter) -> None:
    run = _Run(rig, emitter)

    run.ingest()

    assert run.read_graph.queries
    assert all(q.lstrip().startswith("MATCH") and "MERGE" not in q for q in run.read_graph.queries)


def test_an_edge_to_a_parent_that_exists_nowhere_is_dropped_and_the_run_still_succeeds(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    ghost = StructuralEdge("ARTICLE", "GHOST#art", "PARAGRAPH", "32024R2847#art_1.001")
    run = _Run(rig, emitter)

    run.ingest(_structure(extra_edges=(ghost,)))

    edges = rig.graphs.open(_GRAPH).edges
    assert not any("GHOST#art" in key for key in edges)
    assert len(edges) == 2


def test_the_four_stage_log_entries_keep_their_names_and_order(
    rig: GatewayRig,
    make_emitter: Callable[..., tuple[LogEmitter, Path]],
    read_lines: Callable[[Path], list[dict[str, object]]],
) -> None:
    emitter, log_path = make_emitter()
    run = _Run(rig, emitter)

    run.ingest()
    emitter.flush()

    actions = [
        line["action"]
        for line in read_lines(log_path)
        if line.get("entity_id") == "CRA-1.0" and line.get("component") == "ingestion"
    ]
    assert actions == [
        "fetch_regulatory_instrument_structure",
        "register_regulatory_instrument_version",
        "persist_native_structural_graph",
        "verify_structural_graph_reachable",
    ]


def test_an_orphan_in_the_graph_is_still_reported_after_the_write(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    run = _Run(rig, emitter)
    run.read_graph = _ReadGraph(orphans_in="ARTICLE")

    with pytest.raises(IngestionPersistenceError, match=r"ARTICLE.*not reachable"):
        run.ingest()

    assert len(rig.store.groups) == 1  # the data is written; the check reports what it found


def test_a_gateway_that_could_not_apply_the_group_fails_the_run_before_the_reachability_check(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    rig.graphs.open(_GRAPH).fail_on_write(redis.exceptions.ConnectionError("down"))
    run = _Run(rig, emitter)

    with pytest.raises(IngestionPersistenceError, match="FalkorDB write failed"):
        run.ingest()

    assert run.read_graph.queries == []
    assert len(rig.store.groups) == 1  # committed to the log: a re-run is idempotent


def test_a_graph_still_behind_its_log_fails_the_run_before_the_reachability_check(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    run = _Run(rig, emitter)
    run.ingest()
    graph = rig.graphs.open(_GRAPH)
    graph.fail_on_write(redis.exceptions.ConnectionError("down"))
    changed = _structure()
    changed = FetchedRegulatoryInstrumentStructure(
        metadata=changed.metadata,
        nodes=(*changed.nodes, StructuralNode("RECITAL", "32024R2847#rct_1", {"text": "r"})),
        edges=(
            *changed.edges,
            StructuralEdge("RegulatoryInstrument", "32024R2847", "RECITAL", "32024R2847#rct_1"),
        ),
    )
    run.read_graph.queries.clear()

    with pytest.raises(IngestionPersistenceError, match="FalkorDB write failed"):
        run.ingest(changed)

    assert run.read_graph.queries == []


def test_an_invalid_element_type_submits_nothing(rig: GatewayRig, emitter: LogEmitter) -> None:
    bad = _structure()
    bad = FetchedRegulatoryInstrumentStructure(
        metadata=bad.metadata,
        nodes=(*bad.nodes, StructuralNode("BOGUS", "32024R2847#bogus", {})),
        edges=bad.edges,
    )
    run = _Run(rig, emitter)

    with pytest.raises(IngestionPersistenceError, match="element_type"):
        run.ingest(bad)

    assert rig.store.groups == []
    assert run.read_graph.queries == []


def test_the_instrument_is_upserted_before_the_structural_nodes(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    run = _Run(rig, emitter)

    run.ingest()

    graph = rig.graphs.open(_GRAPH)
    assert graph.upsert_order == [
        ("RegulatoryInstrument", "CRA-1.0"),
        ("ARTICLE", "32024R2847#art_1"),
        ("PARAGRAPH", "32024R2847#art_1.001"),
    ]


def test_a_re_ingest_with_fewer_properties_keeps_the_ones_the_graph_already_has(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    run = _Run(rig, emitter)
    run.ingest()
    first = _structure()
    without_celex = RegulatoryInstrumentMetadata(
        title=first.metadata.title,
        jurisdiction=first.metadata.jurisdiction,
        effective_date=first.metadata.effective_date,
        version=first.metadata.version,
        status=first.metadata.status,
        source_type=first.metadata.source_type,
        instrument_type=first.metadata.instrument_type,
        celex=None,
    )

    run.ingest(
        FetchedRegulatoryInstrumentStructure(
            metadata=without_celex, nodes=first.nodes, edges=first.edges
        )
    )

    instrument = rig.graphs.open(_GRAPH).nodes[("RegulatoryInstrument", "CRA-1.0")]
    assert instrument["celex"] == "32024R2847"


def test_the_native_group_requests_a_checkpoint_so_the_graph_has_a_head_checkpoint(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    """AC-BI-009: the native group is the terminal group of an external ingest (#215, S4b)."""
    _Run(rig, emitter).ingest()

    assert_head_checkpoints(rig, [_GRAPH])


def test_a_checkpoint_that_cannot_be_recorded_does_not_fail_the_ingest(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    rig.store.checkpoint_fault = Fault(redis.exceptions.ConnectionError("down"))

    _Run(rig, emitter).ingest()

    assert ("RegulatoryInstrument", "CRA-1.0") in rig.graphs.open(_GRAPH).nodes
    assert_no_checkpoint(rig)
