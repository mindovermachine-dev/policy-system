"""Real, freshly ingested CRA/GDPR/NIS2 native graphs for the live tests that read them.

The Domain Mapper, Company Merge and Change Monitor live tests start from the native graphs the
Ingestion component writes. Reading whatever `cra_native`/`gdpr_native`/`nis2_native` happened to be
in the developer's FalkorDB would fail on any clean instance. The
`live_native_graphs` fixture ingests the three instruments from the live Cellar/ELI service
through the real Graph Write Gateway on the scratch PS Postgres (the `provisioned_graph_log`
fixture), and deletes the native and baseline graphs of all three before and after, so a run never
depends on, or leaves behind, ambient state.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from ps_service.domain_mapper.adapters.cellar_eli import CellarEliDomainMappingAdapter
from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.extraction import extract_roles_and_requirements
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.ingestion.adapters.cellar_eli import CellarEliAdapter
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.ingestion.pipeline import ingest_regulatory_instrument
from ps_service.logging import EmitterConfig, LogEmitter, bind_run_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persistence.provisioned_postgres import Provisioned

    from ps_service.config import ServiceConfig
    from ps_service.domain_mapper.adapters.base import DomainMappingAdapter
    from ps_service.domain_mapper.falkordb_client import GraphHandle
    from ps_service.domain_mapper.models import ExtractionUnit

# short name -> CELEX identifier of the three instruments the live suites use.
LIVE_INSTRUMENTS = {"CRA": "32024R2847", "NIS2": "32022L2555", "GDPR": "32016R0679"}
_VERSION = "1.0"
_EXTRACTION_UNIT_LIMIT = 15  # bounds LLM cost/time, as the live capstones always have

# Captured at import time, before the autouse `_isolate_logging` fixture strips it per test.
_CHAT_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")


def _graph_names() -> tuple[str, ...]:
    return tuple(
        f"{short_name.lower()}_{kind}"
        for short_name in LIVE_INSTRUMENTS
        for kind in ("native", "baseline")
    )


@pytest.fixture(scope="module")
def live_native_graphs(
    provisioned_graph_log: Provisioned, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ServiceConfig]:
    """Ingest CRA, NIS2 and GDPR natively from Cellar; yield the config that reaches the stores.

    The yielded config points at the scratch PS state Postgres (graph log and audit trail), for the
    live tests that build their own gateway over it.
    """
    config = provisioned_graph_log.state_config()
    db = connect_from_config(config)
    existing = set(db.list_graphs())
    for name in _graph_names():
        if name in existing:
            db.select_graph(name).delete()

    log_path: Path = tmp_path_factory.mktemp("live_native") / "ingestion.jsonl"
    emitter = LogEmitter(EmitterConfig(log_path=log_path))
    adapter = CellarEliAdapter()
    gateway = build_default_graph_write_gateway(config)
    record_audit = build_system_audit_recorder(config)
    try:
        for short_name, celex in LIVE_INSTRUMENTS.items():
            ingest_regulatory_instrument(
                celex,
                short_name,
                version=_VERSION,
                adapter=adapter,
                graph=select_graph(db, native_graph_name(short_name)),
                submitter=gateway,
                audit_event_id=record_audit(
                    action="ingestion_run.submit",
                    resource_type="ingestion_run",
                    resource_id=f"live-native-{short_name}",
                    details={"status": "started", "short_name": short_name},
                ),
                emitter=emitter,
            )
        emitter.flush()
        yield config
    finally:
        emitter.stop()
        for name in _graph_names():
            if name in set(db.list_graphs()):
                db.select_graph(name).delete()


class _LimitedDomainMappingAdapter:
    """Wraps a real `DomainMappingAdapter`, capping the units it returns to the first `limit`."""

    def __init__(self, inner: DomainMappingAdapter, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    def read_native_units(
        self, graph: GraphHandle, regulatory_instrument_id: str
    ) -> tuple[ExtractionUnit, ...]:
        return self._inner.read_native_units(graph, regulatory_instrument_id)[: self._limit]


@pytest.fixture(scope="module")
def live_baseline_graphs(
    live_native_graphs: ServiceConfig, tmp_path_factory: pytest.TempPathFactory
) -> ServiceConfig:
    """Run bounded Domain Mapper extraction and derivation over the fresh native graphs.

    Needs the real LLM provider (`PS_LLMINTERFACE_MODEL`, `AZURE_*`) -- the dependent tests carry
    `llm_live`. Populates `cra_baseline`/`gdpr_baseline`/`nis2_baseline`; `live_native_graphs`
    deletes them again when the module ends. Yields the same config as `live_native_graphs`.
    """
    assert _CHAT_MODEL, "live_baseline_graphs needs PS_LLMINTERFACE_MODEL set at process start"
    config = live_native_graphs
    db = connect_from_config(config)
    gateway = build_default_graph_write_gateway(config)
    record_audit = build_system_audit_recorder(config)
    adapter = _LimitedDomainMappingAdapter(CellarEliDomainMappingAdapter(), _EXTRACTION_UNIT_LIMIT)
    emitter = LogEmitter(
        EmitterConfig(log_path=tmp_path_factory.mktemp("live_baseline") / "mapper.jsonl")
    )
    try:
        for short_name in LIVE_INSTRUMENTS:
            instrument_id = f"{short_name}-{_VERSION}"
            for stage in ("extraction", "derivation"):
                run_id = f"live-baseline-{short_name.lower()}-{stage}"
                keywords = {
                    "submitter": gateway,
                    "baseline_graph_name": baseline_graph_name(short_name),
                    "audit_event_id": record_audit(
                        action="ingestion_run.submit",
                        resource_type="ingestion_run",
                        resource_id=run_id,
                        details={"trigger": "live_baseline"},
                    ),
                    "model": _CHAT_MODEL,
                    "emitter": emitter,
                }
                with bind_run_context(run_id):
                    if stage == "extraction":
                        extract_roles_and_requirements(
                            instrument_id,
                            adapter=adapter,
                            native_graph=select_graph(db, native_graph_name(short_name)),
                            **keywords,  # pyright: ignore[reportArgumentType]
                        )
                    else:
                        derive_obligations_and_capabilities(
                            instrument_id,
                            baseline_graph=select_graph(db, baseline_graph_name(short_name)),
                            **keywords,  # pyright: ignore[reportArgumentType]
                        )
    finally:
        emitter.stop()
    return config
