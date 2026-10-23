"""`trigger_reingestion` mirrors the succession on the single-tenant graph it is given."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from change_monitor._fakes import FakeAdapter
from change_monitor._gateway_fakes import RecordingSubmitter
from ps_service.change_monitor.trigger import trigger_reingestion
from ps_service.ingestion.models import (
    FetchedRegulatoryInstrumentStructure,
    RegulatoryInstrumentMetadata,
)

if TYPE_CHECKING:
    from change_monitor._fakes import MakeEmitter
    from change_monitor._gateway_fakes import SuccessionRig
    from ps_service.change_monitor.models import PipelineRunResult

_IDENTIFIER = "32024R2847"
_PRIOR = "CRA-1.0"
_NEW = "CRA-2.0"
_CUSTOM = "custom_graph"


def _structure() -> FetchedRegulatoryInstrumentStructure:
    metadata = RegulatoryInstrumentMetadata(
        title="Fixture",
        jurisdiction="EU",
        effective_date=date(2027, 12, 11),
        version="1.0",
        status="active",
        source_type="external",
        instrument_type="regulation",
    )
    return FetchedRegulatoryInstrumentStructure(metadata=metadata, nodes=(), edges=())


def test_trigger_reingestion_mirrors_the_succession_on_the_named_single_tenant_graph(
    succession_rig: SuccessionRig, make_emitter: MakeEmitter
) -> None:
    rig = succession_rig
    custom = rig.rig.graphs.open(_CUSTOM)
    rig.seed_instrument(rig.native, _PRIOR)
    rig.seed_instrument(rig.native, _NEW)
    rig.seed_instrument(custom, _PRIOR)
    rig.seed_instrument(custom, _NEW)
    for stage in ("ingestion", "extraction", "derivation", "merge"):
        rig.seed_marker(rig.native, _NEW, stage)
    emitter, _ = make_emitter()
    submitter = RecordingSubmitter(rig.rig.gateway)
    audit_event_id = rig.new_audit_event_id()

    def _no_stage(**_: object) -> PipelineRunResult:
        raise AssertionError("no stage is outstanding")

    trigger_reingestion(
        _IDENTIFIER,
        "CRA",
        "2.0",
        adapter=FakeAdapter({_IDENTIFIER: _structure()}),
        graph=rig.read_native(),
        submitter=submitter,
        single_tenant_graph_name=_CUSTOM,
        audit_anchor=lambda: audit_event_id,
        emitter=emitter,  # pyright: ignore[reportArgumentType]
        run_pipeline=_no_stage,
    )

    assert rig.status_of(custom, _PRIOR) == "superseded"
    assert submitter.ops(_CUSTOM) != []
    assert submitter.ops("policy_system") == []
