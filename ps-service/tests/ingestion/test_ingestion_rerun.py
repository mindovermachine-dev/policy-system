"""A rerun after an interrupted ingestion equals an uninterrupted run (#208, AC-BI-005).

Group append is atomic and the gateway applies from its marker, so a run that died between the
log and the graph (or between its two internal groups) is completed by running it again: the
graphs end up with the digests of a run nobody interrupted, a group that had already taken
effect is `unchanged`, and the reachability check still reports orphans as it always did.
Postgres and FalkorDB are the gateway rig's in-memory fakes.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from typing import TYPE_CHECKING

import pytest
import redis.exceptions
from graph_gateway._fakes import GatewayRig

from ingestion.adapters.internal_seed._seeds import (
    BASELINE_GRAPH,
    NATIVE_GRAPH,
    RecordingSubmitter,
    Run,
    full_seed,
)
from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.errors import GraphLogUnavailableError
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
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

    from ps_service.graph_gateway.models import GroupOutcome, MutationGroup
    from ps_service.logging import LogEmitter

_DOWN = redis.exceptions.ConnectionError("down")
_EXTERNAL_GRAPH = "cra_native"


def _digests(run: Run) -> tuple[str, str]:
    return canonical_digest(run.native), canonical_digest(run.baseline)


@pytest.fixture
def reference_digests() -> tuple[str, str]:
    """`(native, baseline)` digests of a full seed ingested once, without interruption."""
    reference = GatewayRig()
    try:
        run = Run(reference)
        run.ingest(full_seed())
        return _digests(run)
    finally:
        reference.close()


class _DiesBeforeTheBaseline(RecordingSubmitter):
    """A submitter whose log store becomes unreachable once the native group is in."""

    def submit_group(self, group: MutationGroup) -> GroupOutcome:
        if group.graph == BASELINE_GRAPH:
            raise GraphLogUnavailableError
        return super().submit_group(group)


def test_a_native_group_whose_apply_died_midway_is_completed_by_the_rerun(
    rig: GatewayRig, reference_digests: tuple[str, str]
) -> None:
    run = Run(rig)
    run.native.fail_after_n_writes(2, _DOWN)
    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        run.ingest(full_seed())
    run.native.heal()

    run.ingest(full_seed())

    assert _digests(run) == reference_digests


def test_a_baseline_group_that_failed_leaves_the_native_group_unchanged_on_the_rerun(
    rig: GatewayRig, reference_digests: tuple[str, str]
) -> None:
    run = Run(rig)
    rig.graphs.open(BASELINE_GRAPH).fail_on_write(_DOWN)
    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        run.ingest(full_seed())
    run.baseline.heal()
    native_groups = [g for g in rig.store.groups if g.graph == NATIVE_GRAPH]

    run.ingest(full_seed())

    assert _digests(run) == reference_digests
    assert [g for g in rig.store.groups if g.graph == NATIVE_GRAPH] == native_groups
    assert [g.graph for g in rig.store.groups].count(BASELINE_GRAPH) == 1


def test_a_run_that_never_reached_the_baseline_group_is_completed_by_the_rerun(
    rig: GatewayRig, reference_digests: tuple[str, str]
) -> None:
    run = Run(rig)
    run.submitter = _DiesBeforeTheBaseline(rig.gateway)
    with pytest.raises(InternalSeedError, match=r"^FalkorDB write failed: "):
        run.ingest(full_seed())
    assert [g.graph for g in rig.store.groups] == [NATIVE_GRAPH]
    run.submitter = RecordingSubmitter(rig.gateway)

    run.ingest(full_seed())

    assert _digests(run) == reference_digests
    assert [g.graph for g in rig.store.groups] == [NATIVE_GRAPH, BASELINE_GRAPH]
    assert [g.graph for g in run.submitter.groups] == [NATIVE_GRAPH, BASELINE_GRAPH]


# --- external path ---


class _Rows:
    def __init__(self, value: int) -> None:
        self.result_set: list[object] = [[value]]


class _ReadGraph:
    """A read handle answering every count with `0`, or reporting orphans of one label."""

    def __init__(self, orphans_in: str | None = None) -> None:
        self._orphans_in = orphans_in

    def query(self, q: str, params: dict[str, object] | None = None) -> _Rows:
        del params
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


def _structure(articles: int = 3) -> FetchedRegulatoryInstrumentStructure:
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
    nodes: list[StructuralNode] = []
    edges: list[StructuralEdge] = []
    for number in range(1, articles + 1):
        article, paragraph = f"32024R2847#art_{number}", f"32024R2847#art_{number}.001"
        nodes += [
            StructuralNode("ARTICLE", article, props),
            StructuralNode("PARAGRAPH", paragraph, props),
        ]
        edges += [
            StructuralEdge("RegulatoryInstrument", "32024R2847", "ARTICLE", article),
            StructuralEdge("ARTICLE", article, "PARAGRAPH", paragraph),
        ]
    return FetchedRegulatoryInstrumentStructure(
        metadata=metadata, nodes=tuple(nodes), edges=tuple(edges)
    )


def _ingest(
    rig: GatewayRig,
    emitter: LogEmitter,
    structure: FetchedRegulatoryInstrumentStructure,
    *,
    read_graph: _ReadGraph | None = None,
) -> None:
    audit_event_id = str(uuid.uuid4())
    rig.store.audit_rows.add(audit_event_id)
    ingest_regulatory_instrument(
        "32024R2847",
        "CRA",
        version="1.0",
        adapter=_Adapter(structure),
        graph=read_graph or _ReadGraph(),
        submitter=rig.gateway,
        audit_event_id=audit_event_id,
        emitter=emitter,
    )


@pytest.fixture
def emitter(make_emitter: Callable[..., tuple[LogEmitter, Path]]) -> LogEmitter:
    """The run-log emitter every ingestion here writes through (its file is not read)."""
    return make_emitter()[0]


def test_an_external_apply_that_died_midway_is_completed_by_the_rerun(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    reference = GatewayRig()
    try:
        _ingest(reference, emitter, _structure())
        expected = canonical_digest(reference.graphs.open(_EXTERNAL_GRAPH))
    finally:
        reference.close()
    rig.graphs.open(_EXTERNAL_GRAPH).fail_after_n_writes(1, _DOWN)
    with pytest.raises(IngestionPersistenceError, match="FalkorDB write failed"):
        _ingest(rig, emitter, _structure())
    rig.graphs.open(_EXTERNAL_GRAPH).heal()

    _ingest(rig, emitter, _structure())

    assert canonical_digest(rig.graphs.open(_EXTERNAL_GRAPH)) == expected


def test_an_orphan_is_reported_on_the_rerun_exactly_as_on_a_first_run(
    rig: GatewayRig, emitter: LogEmitter
) -> None:
    rig.graphs.open(_EXTERNAL_GRAPH).fail_on_write(_DOWN)
    with pytest.raises(IngestionPersistenceError, match="FalkorDB write failed"):
        _ingest(rig, emitter, _structure())
    rig.graphs.open(_EXTERNAL_GRAPH).heal()

    with pytest.raises(IngestionPersistenceError, match=r"ARTICLE.*not reachable"):
        _ingest(rig, emitter, _structure(), read_graph=_ReadGraph(orphans_in="ARTICLE"))


def test_a_cra_sized_native_graph_is_one_group_and_its_commit_time_is_recorded(
    rig: GatewayRig, emitter: LogEmitter, record_property: Callable[[str, object], None]
) -> None:
    articles = 1500  # 3000 nodes, 3000 edges: the order of magnitude of a CRA-sized instrument
    structure = _structure(articles)

    started = time.perf_counter()
    _ingest(rig, emitter, structure)
    elapsed = time.perf_counter() - started

    record_property("native_group_primitives", len(rig.store.groups[0].entries))
    record_property("native_group_seconds_in_memory_rig", round(elapsed, 3))
    assert len(rig.store.groups) == 1
    assert len(rig.graphs.open(_EXTERNAL_GRAPH).nodes) == 2 * articles + 1
    assert len(rig.graphs.open(_EXTERNAL_GRAPH).edges) == 2 * articles
