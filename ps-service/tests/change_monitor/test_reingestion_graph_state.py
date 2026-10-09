"""Hermetic graph-level proof of an amendment re-ingest, on stateful graphs (issue #201).

Entry is the real `run_change_check_sweep`; the code under test includes the REAL
`will_reingest` / `trigger_reingestion` / `classify_reingestion` / `succession` and the REAL
shared stage sequence (`_execute_catalog_stages`). The boundaries are doubles: the three graphs
(`{short}_native`, `{short}_baseline`, `policy_system`) are in-memory `StatefulGraphSet`
graphs with MERGE-by-key semantics, and the four stages are `StatefulStages`, which apply real
keyed writes for the version they are given (only the LLM / Cellar boundary is absent).

Honest limit: this file proves orchestration, partial-failure retry and idempotency at the
graph level. That each stage reads ONLY its own version's inputs (version scoping) is proven by
the Domain Mapper tests (`test_extraction.py`, `test_derivation.py`, `test_cellar_eli.py`) and
by the live capstone, not here: the stage doubles are scoped by construction.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import TYPE_CHECKING

import pytest
from api._audit_fakes import InMemoryAuditStore
from api._fakes import (
    FakeDomainMappingAdapter,
    FakeIngestionAdapter,
    FakeInternalSeedAdapter,
    build_fake_change_check_dependencies,
)

from change_monitor._fakes import (
    FakeAdapter,
    StatefulGraphSet,
    StatefulStages,
    VersionContent,
)
from change_monitor.test_trigger import (
    _structure,  # pyright: ignore[reportPrivateUsage]  -- reuse the trigger tests' canned metadata
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import (
    ChangeCheckDependencies,
    ChangeCheckResult,
    run_change_check_sweep,
)
from ps_service.api.ingestion_orchestration import (
    GraphOpeners,
    PipelineAdapters,
    PipelineDependencies,
)
from ps_service.audit import AuditContext
from ps_service.change_monitor.models import AmendmentFinding, PollReport, TrackedInstrumentNode
from ps_service.change_monitor.trigger import trigger_reingestion, will_reingest
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
    from api._fakes import MakeEmitter

    from change_monitor._fakes import GraphSnapshot

_CELEX = "32024R2847"
_NEW_CELEX = "32024R2847C01"
_PRIOR = "CRA-1.0"
_NEW = f"CRA-{_NEW_CELEX}"
_ALL = ("ingestion", "extraction", "derivation", "merge")
_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    llm_interface_model="azure/gpt-4o",
    llm_interface_embed_model="azure/text-embedding-3-large",
    company_merge_similarity_threshold=0.83,
)
_CONTENT = {
    _PRIOR: VersionContent(obligations=("A",)),
    _NEW: VersionContent(obligations=("A", "B"), capabilities=("C",)),
}


def _seeded() -> tuple[StatefulGraphSet, StatefulStages]:
    """The three graphs holding the fully mapped and merged prior version `CRA-1.0`."""
    graphs = StatefulGraphSet()
    stages = StatefulStages(graphs, _CONTENT)
    stages.run_all(_CELEX, "CRA", "1.0")
    graphs.events.clear()
    return graphs, stages


def _sweep(
    graphs: StatefulGraphSet,
    stages: StatefulStages,
    emitter: object,
    store: InMemoryAuditStore | None = None,
) -> ChangeCheckResult:
    fake = build_fake_change_check_dependencies(
        tracked=(
            TrackedInstrumentNode(
                regulatory_instrument_id=_PRIOR,
                celex=_CELEX,
                instrument_type="regulation",
                effective_date="2024-01-01",
            ),
        ),
        poll_report=PollReport(
            findings=(
                AmendmentFinding(
                    regulatory_instrument_id=_PRIOR,
                    instrument_type="regulation",
                    baseline_reference="2024-01-01",
                    detected_consolidated_celex=_NEW_CELEX,
                    detected_consolidation_date=date(2025, 1, 1),
                    reason="newer_consolidation",
                ),
            ),
            polled_count=1,
            failed_ids=(),
            unconfigured_ids=(),
        ),
        catalog_entries={
            _CELEX: CatalogEntry(celex=_CELEX, title="CRA", short_name="CRA", version="1.0")
        },
        reingestion_result=None,
    )
    deps: ChangeCheckDependencies = dataclasses.replace(
        fake.dependencies,
        pipeline=PipelineDependencies(
            graphs=GraphOpeners(
                native=lambda _config, _short: graphs.native,  # pyright: ignore[reportUnknownLambdaType]
                baseline=lambda _config, _short: graphs.baseline,  # pyright: ignore[reportUnknownLambdaType]
                single_tenant=lambda _config: graphs.single_tenant,  # pyright: ignore[reportUnknownLambdaType]
            ),
            stages=stages.pipeline_stages(),
            adapters=PipelineAdapters(
                ingestion=FakeIngestionAdapter,
                mapping=FakeDomainMappingAdapter,
                internal_seed=FakeInternalSeedAdapter,
            ),
        ),
        open_single_tenant=lambda _config: graphs.single_tenant,  # pyright: ignore[reportUnknownLambdaType]
        open_native=lambda _config, _short: graphs.native,  # pyright: ignore[reportUnknownLambdaType]
        default_adapter=lambda: FakeAdapter({_CELEX: _structure()}),
        trigger_reingestion=trigger_reingestion,  # pyright: ignore[reportArgumentType]
        will_reingest=will_reingest,  # pyright: ignore[reportArgumentType]
    )
    return run_change_check_sweep(
        config=_CONFIG,
        run_id="sweep",
        dependencies=deps,
        audit=AuditContext(
            ("caller", "https://issuer.example.com/"), store or InMemoryAuditStore()
        ),
        emitter=emitter,  # pyright: ignore[reportArgumentType]
    )


def _all_snapshots(graphs: StatefulGraphSet) -> tuple[GraphSnapshot, GraphSnapshot, GraphSnapshot]:
    return graphs.native.snapshot(), graphs.baseline.snapshot(), graphs.single_tenant.snapshot()


def test_amendment_adds_new_obligation_and_capability_to_single_tenant_graph(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-002 (+ AC-BI-001 order, AC-BI-003 succession after merge, AC-BI-011 counts)."""
    emitter, _ = make_emitter()
    graphs, stages = _seeded()
    assert graphs.single_tenant.count_nodes("Obligation") == 1
    assert graphs.single_tenant.count_nodes("Capability") == 0
    store = InMemoryAuditStore()

    result = _sweep(graphs, stages, emitter, store)

    [outcome] = result.instruments
    assert (outcome.outcome, outcome.detail) == (
        "amendment_reingested",
        f"{_NEW} (superseded, fresh)",
    )
    tenant = graphs.single_tenant
    assert {node_id for label, node_id in tenant.nodes if label == "Obligation"} == {
        "OBL-A",
        "OBL-B",
    }
    assert {node_id for label, node_id in tenant.nodes if label == "Capability"} == {"CAP-C"}
    # AC-BI-001 / AC-BI-003: the four stages in order, and no succession write before merge.
    stage_and_link = [e for e in graphs.events if e.startswith("stage:") or "SUPERSEDED_BY" in e]
    assert stage_and_link == [f"stage:{stage}" for stage in _ALL] + [
        "write:SUPERSEDED_BY",
        "write:policy_system:SUPERSEDED_BY",
    ]
    assert graphs.native.edges[("SUPERSEDED_BY", _PRIOR, _NEW)] == {"absorbed": True}
    assert graphs.native.marker_stage(_NEW) is None
    # AC-BI-011: the audited counts are the graph delta (A already existed, B and C are new).
    complete = store.rows[1].details
    assert (complete["new_obligations"], complete["new_capabilities"]) == (1, 1)


def test_prior_version_nodes_edges_and_properties_unchanged_except_status_and_succession_edge(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-004: nothing the prior version owns is modified or deleted.

    The one intended exception (AC-BI-003): the sweep flips the prior `RegulatoryInstrument`'s
    `status` active -> superseded in the native and `policy_system` graphs and adds the one
    `SUPERSEDED_BY` edge there (the native edge also carries `absorbed`). Every other node, edge
    and property present before the sweep is equal afterwards, in all three graphs.
    """
    emitter, _ = make_emitter()
    graphs, stages = _seeded()
    before = _all_snapshots(graphs)

    _sweep(graphs, stages, emitter)

    after = _all_snapshots(graphs)
    flipped = ("RegulatoryInstrument", _PRIOR)
    for index, (was, now) in enumerate(zip(before, after, strict=True)):
        for key, props in was.nodes.items():
            expected = {**props, "status": "superseded"} if key == flipped and index != 1 else props
            assert now.nodes[key] == expected, key
        for key, props in was.edges.items():
            assert now.edges[key] == props, key
    # Baseline: the prior's whole subtree is untouched, status included.
    assert after[1].nodes[flipped]["status"] == "active"
    # The only edges that are new in an existing graph are the new version's own and the succession.
    for index in (0, 2):
        added = set(after[index].edges) - set(before[index].edges)
        assert ("SUPERSEDED_BY", _PRIOR, _NEW) in added
        assert all(_NEW in key[1:] or key[0] == "SUPERSEDED_BY" for key in added)


@pytest.mark.parametrize("stage", _ALL)
def test_retry_after_partial_failure_creates_no_duplicates(
    stage: str, make_emitter: MakeEmitter
) -> None:
    """AC-BI-008: a stage that wrote half its nodes then raised leaves the prior active, and the
    next sweep re-runs it without duplicating anything: the three graphs end exactly as a run
    that never failed (node and edge counts included).
    """
    emitter, _ = make_emitter()
    clean_graphs, clean_stages = _seeded()
    _sweep(clean_graphs, clean_stages, emitter)

    graphs, stages = _seeded()
    stages.arm_failure(stage, partial=True)
    first = _sweep(graphs, stages, emitter)

    assert [o.outcome for o in first.instruments] == ["reingest_failed"]
    assert graphs.native.status_of(_PRIOR) == "active"
    assert graphs.single_tenant.status_of(_PRIOR) == "active"
    assert ("SUPERSEDED_BY", _PRIOR, _NEW) not in graphs.native.edges
    assert stages.failures_fired == 1

    second = _sweep(graphs, stages, emitter)

    assert [o.outcome for o in second.instruments] == ["amendment_reingested"]
    retried = _all_snapshots(graphs)
    clean = _all_snapshots(clean_graphs)
    for got, want in zip(retried, clean, strict=True):
        assert got.nodes == want.nodes
        assert got.edges == want.edges
    assert graphs.single_tenant.count_nodes("Obligation") == 2


def test_already_processed_sweep_touches_no_graph(make_emitter: MakeEmitter) -> None:
    """AC-BI-009: once absorbed, a further sweep issues no stage and no graph write at all."""
    emitter, _ = make_emitter()
    graphs, stages = _seeded()
    _sweep(graphs, stages, emitter)
    settled = _all_snapshots(graphs)
    graphs.events.clear()
    native_writes, tenant_writes = len(graphs.native.writes), len(graphs.single_tenant.writes)
    store = InMemoryAuditStore()

    result = _sweep(graphs, stages, emitter, store)

    [outcome] = result.instruments
    assert (outcome.outcome, outcome.detail) == (
        "amendment_reingested",
        f"{_NEW} (already_processed)",
    )
    assert graphs.events == []
    assert store.rows == []
    assert (len(graphs.native.writes), len(graphs.single_tenant.writes)) == (
        native_writes,
        tenant_writes,
    )
    for got, want in zip(_all_snapshots(graphs), settled, strict=True):
        assert got.nodes == want.nodes
        assert got.edges == want.edges


def _legacy_ingestion_only_state(graphs: StatefulGraphSet, stages: StatefulStages) -> None:
    """The graph exactly as the earlier ingestion-only sweep left it (AC-BI-010).

    The new version was ingested into native and the old code then wrote the succession: the
    prior flipped to `superseded` in native only, and the `SUPERSEDED_BY` edge has NO `absorbed`
    property. Nothing was extracted, derived or merged, and `policy_system` still shows the
    prior as active.
    """
    stages.ingest(_CELEX, "CRA", version=_NEW_CELEX)
    graphs.native.merge_edge("SUPERSEDED_BY", _PRIOR, _NEW)
    graphs.native.nodes[("RegulatoryInstrument", _PRIOR)]["status"] = "superseded"
    graphs.events.clear()


def test_repair_runs_extract_derive_merge_once_for_legacy_edge_and_second_sweep_runs_nothing(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-010: a legacy-linked new version is mapped and merged once, then left alone."""
    emitter, _ = make_emitter()
    graphs, stages = _seeded()
    _legacy_ingestion_only_state(graphs, stages)
    store = InMemoryAuditStore()

    first = _sweep(graphs, stages, emitter, store)

    [outcome] = first.instruments
    assert (outcome.outcome, outcome.detail) == (
        "amendment_reingested",
        f"{_NEW} (superseded, repair)",
    )
    assert [e for e in graphs.events if e.startswith("stage:")] == [
        "stage:extraction",
        "stage:derivation",
        "stage:merge",
    ]
    assert graphs.native.edges[("SUPERSEDED_BY", _PRIOR, _NEW)] == {"absorbed": True}
    assert graphs.single_tenant.status_of(_PRIOR) == "superseded"
    assert {node_id for label, node_id in graphs.single_tenant.nodes if label == "Obligation"} == {
        "OBL-A",
        "OBL-B",
    }
    assert graphs.native.marker_stage(_NEW) is None
    assert [row.action for row in store.rows] == ["ingestion_run.submit", "ingestion_run.complete"]
    settled = _all_snapshots(graphs)
    graphs.events.clear()
    second_store = InMemoryAuditStore()

    second = _sweep(graphs, stages, emitter, second_store)

    assert second.instruments[0].detail == f"{_NEW} (already_processed)"
    assert graphs.events == []
    assert second_store.rows == []
    for got, want in zip(_all_snapshots(graphs), settled, strict=True):
        assert got.nodes == want.nodes
        assert got.edges == want.edges


def test_repair_that_fails_at_derive_resumes_at_derive_and_ends_as_a_clean_repair(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    graphs, stages = _seeded()
    _legacy_ingestion_only_state(graphs, stages)
    stages.arm_failure("derivation", partial=True)

    first = _sweep(graphs, stages, emitter)

    assert [o.outcome for o in first.instruments] == ["reingest_failed"]
    assert graphs.native.marker_stage(_NEW) == "extraction"
    assert graphs.native.edges[("SUPERSEDED_BY", _PRIOR, _NEW)] == {}
    assert graphs.single_tenant.status_of(_PRIOR) == "active"
    graphs.events.clear()

    second = _sweep(graphs, stages, emitter)

    assert second.instruments[0].detail == f"{_NEW} (superseded, repair)"
    assert [e for e in graphs.events if e.startswith("stage:")] == [
        "stage:derivation",
        "stage:merge",
    ]
    assert graphs.native.edges[("SUPERSEDED_BY", _PRIOR, _NEW)] == {"absorbed": True}
    assert graphs.native.marker_stage(_NEW) is None
