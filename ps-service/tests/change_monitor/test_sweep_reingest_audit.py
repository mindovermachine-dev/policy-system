"""The sweep's audited counts equal the graph delta of an amended re-ingest.

Runs the REAL `will_reingest` / `trigger_reingestion` / `succession` over the gateway rig's native
and `policy_system` graphs with the hand-written pipeline stage doubles, and a single-tenant
graph double that counts the Obligation and Capability nodes the merge stage writes to it. The
delta observed in that graph must equal the counts recorded on the `ingestion_run.complete` row.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import TYPE_CHECKING

from api._fakes import (
    LedgerStageRecorder,
    build_fake_change_check_dependencies,
    build_fake_pipeline_dependencies,
)

from change_monitor._fakes import FakeAdapter, FakeQueryResult
from change_monitor.test_trigger import (
    _IDENTIFIER,  # pyright: ignore[reportPrivateUsage]  -- reuse the trigger tests' fixtures verbatim
    _PRIOR_ID,  # pyright: ignore[reportPrivateUsage]  -- same reuse
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

    from change_monitor._gateway_fakes import SuccessionRig


_CONFIG = ServiceConfig(
    host="127.0.0.1",
    port=8000,
    graceful_shutdown_seconds=10,
    logging_dir=None,
    llm_interface_model="azure/gpt-4o",
    llm_interface_embed_model="azure/text-embedding-3-large",
    company_merge_similarity_threshold=0.83,
)
_NEW_VERSION = "32024R2847C01"
_NEW_ID = f"CRA-{_NEW_VERSION}"


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
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    emitter, _ = make_emitter()
    single_tenant = _CountingSingleTenantGraph()
    before = dict(single_tenant.nodes)
    rig = succession_rig
    rig.seed_instrument(rig.native, _PRIOR_ID)
    rig.seed_instrument(rig.single_tenant, _PRIOR_ID)  # Company Merge landed both in policy_system
    rig.seed_instrument(rig.single_tenant, _NEW_ID)
    pipeline = build_fake_pipeline_dependencies(
        rid=_NEW_ID,
        merge_new_obligations=2,
        merge_new_capabilities=1,
        merge_matched_capabilities=3,
        merge_writes_nodes=True,
        recorder=LedgerStageRecorder(
            rig.rig.events, lambda _call: rig.seed_instrument(rig.native, _NEW_ID)
        ),
    )
    node = TrackedInstrumentNode(
        regulatory_instrument_id=_PRIOR_ID,
        celex=_IDENTIFIER,
        instrument_type="regulation",
        effective_date="2024-01-01",
    )
    finding = AmendmentFinding(
        regulatory_instrument_id=_PRIOR_ID,
        instrument_type="regulation",
        baseline_reference="2024-01-01",
        detected_consolidated_celex=_NEW_VERSION,
        detected_consolidation_date=date(2025, 1, 1),
        reason="newer_consolidation",
    )
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
        pipeline=pipeline,
    )
    deps = dataclasses.replace(
        fake.dependencies,
        open_single_tenant=lambda _config: single_tenant,  # pyright: ignore[reportUnknownLambdaType]
        open_native=lambda _config, _short_name: rig.read_native(),  # pyright: ignore[reportUnknownLambdaType]
        default_adapter=lambda: FakeAdapter({_IDENTIFIER: _structure()}),
        trigger_reingestion=trigger_reingestion,  # pyright: ignore[reportArgumentType]
        will_reingest=will_reingest,  # pyright: ignore[reportArgumentType]
    )
    store = rig.audit_store()

    result = run_change_check_sweep(
        config=_CONFIG,
        run_id="sweep",
        dependencies=deps,
        audit=AuditContext(("caller", "https://issuer.example.com/"), store),
        emitter=emitter,
    )

    assert [(o.outcome, o.detail) for o in result.instruments] == [
        ("amendment_reingested", f"{_NEW_ID} (superseded, fresh)")
    ]
    delta = {label: single_tenant.nodes[label] - before[label] for label in before}
    complete = store.rows[1].details
    assert delta == {"Obligation": 2, "Capability": 1}
    assert complete["new_obligations"] == delta["Obligation"]
    assert complete["new_capabilities"] == delta["Capability"]
    assert complete["matched_capabilities"] == 3
    assert rig.status_of(rig.native, _PRIOR_ID) == "superseded"
    assert rig.status_of(rig.single_tenant, _PRIOR_ID) == "superseded"
    assert (
        result.instruments[0].reingest_run_id
        == store.rows[0].resource_id
        == store.rows[1].resource_id
    )
