"""AC-BI-016 / AC-BI-007: the sweep's audited counts equal the graph delta of an amended re-ingest.

Runs the REAL `will_reingest` / `trigger_reingestion` (and through them the real Ingestion
pipeline) over a scripted native graph, with a single-tenant graph double that counts the
Obligation and Capability nodes written to it. The delta observed in that graph must equal the
counts recorded on the `ingestion_run.complete` row.
"""

from __future__ import annotations

import dataclasses
import functools
from datetime import date
from typing import TYPE_CHECKING

from api._audit_fakes import InMemoryAuditStore
from api._fakes import build_fake_change_check_dependencies

from change_monitor._fakes import FakeAdapter, FakeGraph, FakeQueryResult
from change_monitor.test_trigger import (
    _IDENTIFIER,  # pyright: ignore[reportPrivateUsage]  -- reuse the trigger tests' fixtures verbatim
    _PRIOR_ID,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _ingest_completion_results,  # pyright: ignore[reportPrivateUsage]  -- same reuse
    _structure,  # pyright: ignore[reportPrivateUsage]  -- same reuse
)
from ps_service.api.catalog import CatalogEntry
from ps_service.api.change_check_orchestration import run_change_check_sweep
from ps_service.audit import AuditContext
from ps_service.change_monitor.models import AmendmentFinding, PollReport, TrackedInstrumentNode
from ps_service.change_monitor.trigger import trigger_reingestion, will_reingest
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
    from api._fakes import MakeEmitter


_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
)


class _CountingSingleTenantGraph:
    """Single-tenant `GraphHandle` double that counts Obligation/Capability nodes written."""

    def __init__(self) -> None:
        self.nodes = {"Obligation": 4, "Capability": 9}
        self.queries: list[str] = []

    def query(self, q: str, params: dict[str, object] | None = None) -> FakeQueryResult:
        del params
        self.queries.append(q)
        for label in self.nodes:
            if f"count(n:{label})" in q:
                return FakeQueryResult([[self.nodes[label]]])
            if f":{label}" in q and any(w in q.upper() for w in ("MERGE", "CREATE", "SET")):
                self.nodes[label] += 1
        return FakeQueryResult([])


def test_amendment_reingest_counts_equal_the_obligation_and_capability_delta_in_the_graph(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    single_tenant = _CountingSingleTenantGraph()
    before = dict(single_tenant.nodes)
    node = TrackedInstrumentNode(
        regulatory_instrument_id="CRA-1.0",
        celex=_IDENTIFIER,
        instrument_type="regulation",
        effective_date="2024-01-01",
    )
    finding = AmendmentFinding(
        regulatory_instrument_id="CRA-1.0",
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex="32024R2847C01",
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
    # `will_reingest` and `trigger_reingestion` each run the 3-read preflight on the fresh state.
    preflight = [
        FakeQueryResult([]),
        FakeQueryResult([]),
        FakeQueryResult([[_PRIOR_ID, "regulation"]]),
    ]
    native = FakeGraph([*preflight, *preflight, *_ingest_completion_results()])
    fake = build_fake_change_check_dependencies(
        tracked=(node,),
        poll_report=PollReport(
            findings=(finding,), polled_count=1, failed_ids=(), unconfigured_ids=()
        ),
        catalog_entries={
            _IDENTIFIER: CatalogEntry(
                celex=_IDENTIFIER, title="CRA", short_name="CRA", version="1.0"
            )
        },
        reingestion_result=None,
    )
    deps = dataclasses.replace(
        fake.dependencies,
        open_single_tenant=lambda _config: single_tenant,  # pyright: ignore[reportUnknownLambdaType]
        open_native=lambda _config, _short_name: native,  # pyright: ignore[reportUnknownLambdaType]
        default_adapter=lambda: FakeAdapter({_IDENTIFIER: _structure()}),
        trigger_reingestion=functools.partial(trigger_reingestion, emitter=emitter),  # pyright: ignore[reportArgumentType]
        will_reingest=will_reingest,  # pyright: ignore[reportArgumentType]
    )
    store = InMemoryAuditStore()

    result = run_change_check_sweep(
        config=_CONFIG,
        run_id="sweep",
        dependencies=deps,
        audit=AuditContext(("caller", "https://issuer.example.com/"), store),
        emitter=emitter,
    )

    assert [(o.outcome, o.detail) for o in result.instruments] == [
        ("amendment_reingested", "CRA-32024R2847C01 (superseded)")
    ]
    delta = {label: single_tenant.nodes[label] - before[label] for label in before}
    complete = store.rows[1].details
    assert delta == {"Obligation": 0, "Capability": 0}
    assert complete["new_obligations"] == delta["Obligation"]
    assert complete["new_capabilities"] == delta["Capability"]
    assert complete["matched_capabilities"] == 0
    assert isinstance(native, FakeGraph) and native.writes  # the Ingestion stage did write natively
    assert (
        result.instruments[0].reingest_run_id
        == store.rows[0].resource_id
        == store.rows[1].resource_id
    )
