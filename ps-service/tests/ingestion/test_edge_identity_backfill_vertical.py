"""Re-ingesting onto a legacy-shaped graph adds no parallel edge: the submit path backfills first.

Issue #208, F1. A graph written before the gateway holds edges with no `identity`; the gateway
merges on `identity`, so without the backfill a re-ingest doubles every edge. The ingestion
submit path (`graph_submission.stamp_legacy_edges_before_first_write`) runs the idempotent
backfill before its first group, so nothing here calls it by hand: the edge multiset keeps its
legacy cardinality, a second ingest logs nothing more and the graph's canonical digest equals the
identity-stamped expectation (the digest of the same graph written through the gateway).

Internal seed: the legacy graphs are built by the frozen pre-#208 writer. External: the legacy
shape is the gateway-written graph with its edge identities removed. FalkorDB and Postgres are
the gateway rig's in-memory fakes.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import date
from typing import TYPE_CHECKING

from graph_gateway._fakes import GatewayRig, InMemoryGraph

from ingestion.adapters.internal_seed._legacy_reference import LegacyRecordingGraph, write_legacy
from ingestion.adapters.internal_seed._seeds import Run, full_seed
from ps_service.graph_gateway.digest import canonical_digest
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

    from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
    from ps_service.logging import LogEmitter


def _shape(graph: InMemoryGraph) -> Counter[tuple[str, str, str, str, str]]:
    """The edge multiset of a graph: type and endpoints, identity ignored."""
    return Counter(key[:5] for key in graph.edges)


def _legacy_internal_graphs(
    rig: GatewayRig, seed: InternalRegulationSeed
) -> dict[str, LegacyRecordingGraph]:
    """Install the graphs the pre-#208 writer builds for `seed` (no edge identity) in `rig`."""
    legacy_native, legacy_baseline = LegacyRecordingGraph(), LegacyRecordingGraph()
    write_legacy(seed, legacy_native, legacy_baseline)
    instrument = next(n for n in seed.nodes if n.label == "RegulatoryInstrument")
    short_name = instrument.id.rpartition("-")[0].lower()
    legacy = {f"{short_name}_native": legacy_native, f"{short_name}_baseline": legacy_baseline}
    for name, recorded in legacy.items():
        graph = InMemoryGraph(events=rig.events)
        for (label, node_id), properties in recorded.nodes.items():
            graph.seed_node((label,), node_id, properties)
        for key, properties in recorded.edges.items():
            graph.seed_legacy_edge(key, properties)
        rig.graphs.graphs[name] = graph
    return legacy


def test_internal_seed_reingest_after_backfill_keeps_the_legacy_edge_cardinality(
    rig: GatewayRig,
) -> None:
    seed = full_seed()
    legacy = _legacy_internal_graphs(rig, seed)
    legacy_shape = {name: _shape(rig.graphs.open(name)) for name in legacy}
    run = Run(rig)
    assert all(sum(shape.values()) > 0 for shape in legacy_shape.values())

    run.ingest(seed)

    for name, recorded in legacy.items():
        graph = rig.graphs.open(name)
        assert _shape(graph) == legacy_shape[name]  # no parallel edge
        assert canonical_digest(graph) == canonical_digest(recorded.as_gateway_graph())
    stamps = [g for g in rig.store.groups if g.audit_event_id == run.audit_event_id]
    assert {g.graph for g in stamps} == set(legacy)  # stamp and write share the submit's audit id

    logged = len(rig.store.groups)
    run.ingest(seed)  # second ingest: nothing to stamp, nothing to write
    assert len(rig.store.groups) == logged


def test_a_fresh_graph_logs_no_stamp_group(rig: GatewayRig) -> None:
    run = Run(rig)

    run.ingest(full_seed())

    assert [g.graph for g in rig.store.groups] == [run.native_name, run.baseline_name]


def _structure() -> FetchedRegulatoryInstrumentStructure:
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
        ),
    )


class _Adapter:
    def fetch_regulatory_instrument_structure(
        self, identifier: str
    ) -> FetchedRegulatoryInstrumentStructure:
        del identifier
        return _structure()


class _ReadGraph:
    """A read handle that answers every count with `0` (nothing orphaned)."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _Rows:
        del q, params
        return _Rows()


class _Rows:
    result_set: list[object] = [[0]]  # noqa: RUF012 - read only


def _ingest(rig: GatewayRig, audit_event_id: str, emitter: LogEmitter) -> None:
    ingest_regulatory_instrument(
        "32024R2847",
        "CRA",
        version="1.0",
        adapter=_Adapter(),
        graph=_ReadGraph(),
        submitter=rig.gateway,
        audit_event_id=audit_event_id,
        emitter=emitter,
    )


def test_external_reingest_after_backfill_keeps_the_legacy_edge_cardinality(
    make_emitter: Callable[..., tuple[LogEmitter, Path]],
) -> None:
    emitter = make_emitter()[0]
    fresh = GatewayRig()
    audit_event_id = str(uuid.uuid4())
    fresh.store.audit_rows.add(audit_event_id)
    _ingest(fresh, audit_event_id, emitter)
    expected = canonical_digest(fresh.graphs.open("cra_native"))
    legacy = fresh.graphs.open("cra_native").legacy_shaped_copy()
    legacy_shape = _shape(legacy)
    assert sum(legacy_shape.values()) == 2

    rig = GatewayRig()
    rig.store.audit_rows.add(audit_event_id)
    rig.graphs.graphs["cra_native"] = legacy
    legacy.events = rig.events
    _ingest(rig, audit_event_id, emitter)

    graph = rig.graphs.open("cra_native")
    assert _shape(graph) == legacy_shape
    assert canonical_digest(graph) == expected
    assert {g.audit_event_id for g in rig.store.groups} == {audit_event_id}
    logged = len(rig.store.groups)

    _ingest(rig, audit_event_id, emitter)  # second ingest logs nothing more
    assert len(rig.store.groups) == logged
