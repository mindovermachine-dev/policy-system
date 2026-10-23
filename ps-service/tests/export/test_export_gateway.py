"""Export writes the baseline graph only through the Graph Write Gateway.

Every test enters at `run_export` (the function behind `POST /exports`), runs the REAL
`export_instrument` and embedding backfill over the gateway rig (in-memory log and FalkorDB, the
true boundaries) and asserts on what the rig's graph, log and audit recorder hold.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions
from api._gateway_fakes import (
    ProcessGateway,
    RecordingSystemAudit,
    install_process_gateway,
    process_gateway_installed,
)
from litellm.types.utils import Embedding, EmbeddingResponse

from export._gateway_fakes import ExportRig, ScriptedPolicyGraph
from ps_service.api.errors import ExportStageFailedError, GraphWriteUnavailableError
from ps_service.api.export_orchestration import ExportDependencies, run_export
from ps_service.api.graph_write_access import graph_write_access, install_graph_write_gateway
from ps_service.api.models import ExportRequest
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.config import ServiceConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from company_merge._fakes import MakeEmitter
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.llm_interface.client import EmbeddingCaller
    from ps_service.logging import LogEmitter

_INSTRUMENT_ID = "CRA-1.0"
_PROPERTIES: dict[str, object] = {
    "title": "Cyber Resilience Act",
    "source_type": "external",
    "jurisdiction": "EU",
    "version": "1.0",
    "celex": "32024R2847",
}


class _FakeDb:
    def __init__(self, graphs: list[str]) -> None:
        self.graphs = graphs

    def list_graphs(self) -> list[str]:
        return self.graphs


class _Embedder:
    """The embedding transport: a vector derived from the text, every call recorded."""

    def __init__(
        self,
        rig: ExportRig | None = None,
        audit: RecordingSystemAudit | None = None,
        *,
        fail_on_call: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.events_at_call: list[list[str]] = []
        self.audit_rows_at_call: list[int] = []
        self._rig = rig
        self._audit = audit
        self._fail_on_call = fail_on_call

    def __call__(self, *, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        del timeout
        self.calls.append(inputs[0])
        if self._fail_on_call == len(self.calls):
            message = "the embedding provider is down"
            raise RuntimeError(message)
        if self._rig is not None:
            self.events_at_call.append(list(self._rig.rig.events))
        if self._audit is not None:
            self.audit_rows_at_call.append(len(self._audit.rows))
        digest = hashlib.sha256(inputs[0].encode()).digest()
        vector = [digest[0] / 255.0, digest[1] / 255.0, digest[2] / 255.0]
        return EmbeddingResponse(
            model=model, data=[Embedding(embedding=vector, index=0, object="embedding")]
        )


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_embed_model="fake/embed",
    )


@pytest.fixture
def gateway() -> Iterator[ProcessGateway]:
    """The rig gateway installed as the process's, as the app lifespan does in production."""
    with process_gateway_installed() as installed:
        yield installed


@pytest.fixture
def export_rig(gateway: ProcessGateway) -> ExportRig:
    """The `cra_baseline` / `cra_native` graphs of one external instrument on the rig."""
    rig = ExportRig(gateway.rig)
    rig.seed_node(rig.baseline, "RegulatoryInstrument", _INSTRUMENT_ID, _PROPERTIES)
    rig.seed_node(rig.baseline, "Capability", "cap_1", {"name": "Data Encryption"})
    rig.seed_node(rig.baseline, "Capability", "cap_2", {"name": "Access Control"})
    rig.seed_node(rig.native, "RegulatoryInstrument", _INSTRUMENT_ID, _PROPERTIES)
    return rig


def _dependencies(
    rig: ExportRig,
    *,
    call_embedding: EmbeddingCaller | None,
    opened: list[str] | None = None,
    access: Callable[[ServiceConfig], object] = graph_write_access,
) -> ExportDependencies:
    from ps_service.export.export_instrument import export_instrument

    def open_db(config: ServiceConfig) -> FalkorDB:
        del config
        if opened is not None:
            opened.append("db")
        return cast("FalkorDB", _FakeDb([rig.baseline_name]))

    return ExportDependencies(
        open_db=open_db,
        open_baseline_graph=lambda _db, _short: cast("object", rig.baseline_reader()),  # pyright: ignore[reportArgumentType]
        open_native_graph=lambda _db, _short: cast("object", rig.native_reader()),  # pyright: ignore[reportArgumentType]
        export=export_instrument,
        call_embedding=call_embedding,
        graph_write_access=access,  # pyright: ignore[reportArgumentType]
        open_single_tenant_graph=lambda _db, _name: ScriptedPolicyGraph(),
    )


@pytest.fixture
def emitter(make_emitter: MakeEmitter) -> LogEmitter:
    """A configured log emitter (the Logging boundary), so `run_export` needs no facade."""
    return make_emitter()[0]


def _run(deps: ExportDependencies, emitter: LogEmitter) -> object:
    return run_export(
        ExportRequest.model_validate({"instrument_id": _INSTRUMENT_ID}),
        config=_config(),
        actor="127.0.0.1",
        dependencies=deps,
        emitter=emitter,
    )


# --- the gateway is resolved before any graph is opened ------------------------


def test_export_without_a_gateway_fails_before_any_graph_is_opened(
    export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """No gateway installed -> GraphWriteUnavailableError; nothing opened or computed."""
    install_graph_write_gateway(None)
    embedder = _Embedder(export_rig)
    opened: list[str] = []

    with pytest.raises(GraphWriteUnavailableError):
        _run(_dependencies(export_rig, call_embedding=embedder, opened=opened), emitter)

    assert opened == []
    assert embedder.calls == []
    assert export_rig.rig.store.groups == []
    assert export_rig.baseline_reader().statements == []


# --- one system audit row, recorded first and fail-closed ----------------------


def test_export_records_one_system_audit_row_before_any_embedding_is_computed(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """Exactly one row, naming the instrument, exists before the first embedding call."""
    embedder = _Embedder(export_rig, gateway.system_audit)

    _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert len(gateway.system_audit.rows) == 1
    row = gateway.system_audit.rows[0]
    assert row["action"] == "export.instrument"
    assert row["resource_type"] == "instrument"
    assert row["resource_id"] == _INSTRUMENT_ID
    assert row["details"] == {"instrument_id": _INSTRUMENT_ID, "status": "started"}
    assert embedder.calls != []
    assert set(embedder.audit_rows_at_call) == {1}


def test_export_fails_closed_when_the_system_audit_row_cannot_be_written(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """The row cannot be written -> AuditTrailUnavailableError; nothing is computed."""
    failing = RecordingSystemAudit(
        export_rig.rig,
        error=AuditTrailUnavailableError("The audit trail is temporarily unavailable."),
    )
    install_process_gateway(export_rig.rig, failing)
    embedder = _Embedder(export_rig, failing)

    with pytest.raises(AuditTrailUnavailableError):
        _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert embedder.calls == []
    assert export_rig.rig.store.groups == []
    assert all("embedding" not in props for props in export_rig.baseline.nodes.values())


# --- the embedding backfill is ONE `{short}_baseline` group ---------


def test_the_backfill_computes_every_embedding_before_one_group_is_submitted(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """Compute first; the log holds nothing while any embedding is being computed."""
    embedder = _Embedder(export_rig, gateway.system_audit)

    _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert sorted(embedder.calls) == ["Access Control", "Data Encryption"]
    assert all("log_append" not in events for events in embedder.events_at_call)
    assert export_rig.rig.events.count("log_append") == 1


def test_the_backfill_is_one_embedding_only_group_anchored_to_the_audit_row(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """One group of embedding-only upserts, carrying the recorded row's id."""
    embedder = _Embedder(export_rig, gateway.system_audit)

    _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    groups = export_rig.baseline_groups()
    assert len(groups) == 1
    group = groups[0]
    assert export_rig.rig.store.groups == [group]  # the native graph is never written
    assert [e.content["op"] for e in group.entries] == ["upsert_node", "upsert_node"]
    assert [(e.name, e.identity) for e in group.entries] == [
        ("Capability", "cap_1"),
        ("Capability", "cap_2"),
    ]
    assert all(e.embedding is not None for e in group.entries)
    assert all(e.content.get("properties", {}) == {} for e in group.entries)
    assert group.audit_event_id == gateway.system_audit.ids[0]


def test_the_embeddings_land_on_the_nodes_and_nothing_else_changes(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """The backfill sets `embedding` on nodes that lacked one and keeps their other properties."""
    embedder = _Embedder(export_rig, gateway.system_audit)

    _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    for node_id, name in (("cap_1", "Data Encryption"), ("cap_2", "Access Control")):
        node = export_rig.baseline.nodes[("Capability", node_id)]
        digest = hashlib.sha256(name.encode()).digest()
        assert node["embedding"] == [digest[0] / 255.0, digest[1] / 255.0, digest[2] / 255.0]
        assert node["name"] == name


# --- embedded nodes are untouched, a re-run submits nothing, a failure writes nothing ------


def test_a_node_that_already_has_an_embedding_is_untouched_and_not_logged(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """Only the node lacking an embedding is embedded, submitted and logged."""
    export_rig.seed_node(
        export_rig.baseline, "Capability", "cap_3", {"name": "Key Rotation", "embedding": [0.9]}
    )
    embedder = _Embedder(export_rig, gateway.system_audit)

    _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert "Key Rotation" not in embedder.calls
    assert export_rig.embedding_of("Capability", "cap_3") == [0.9]
    group = export_rig.baseline_groups()[0]
    assert [e.identity for e in group.entries] == ["cap_1", "cap_2"]


def test_a_rerun_embeds_nothing_and_adds_no_log_entry(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """Everything already has an embedding -> no embedding call, no group, no entry."""
    embedder = _Embedder(export_rig, gateway.system_audit)
    _run(_dependencies(export_rig, call_embedding=embedder), emitter)
    calls_after_first = len(embedder.calls)
    log_after_first = [(g.group_id, len(g.entries)) for g in export_rig.rig.store.groups]

    _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert len(embedder.calls) == calls_after_first
    assert [(g.group_id, len(g.entries)) for g in export_rig.rig.store.groups] == log_after_first


def test_a_failed_embedding_call_writes_nothing(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """The second embedding call fails -> no group, no embedding on any node."""
    embedder = _Embedder(export_rig, gateway.system_audit, fail_on_call=2)

    with pytest.raises(ExportStageFailedError):
        _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert len(embedder.calls) == 2
    assert export_rig.rig.store.groups == []
    assert all("embedding" not in props for props in export_rig.baseline.nodes.values())


def test_policy_nodes_are_embedded_only_for_an_internal_source(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """D7: an external source stops at Capability; an internal one also embeds its Policy nodes."""
    export_rig.seed_node(export_rig.baseline, "Policy", "pol_1", {"title": "Encryption Policy"})
    embedder = _Embedder(export_rig, gateway.system_audit)
    _run(_dependencies(export_rig, call_embedding=embedder), emitter)
    assert "Encryption Policy" not in embedder.calls
    assert export_rig.embedding_of("Policy", "pol_1") is None

    internal = ExportRig(gateway.rig, short_name="SOP")
    internal_props = {**_PROPERTIES, "source_type": "internal"}
    internal.seed_node(internal.baseline, "RegulatoryInstrument", "SOP-1.0", internal_props)
    internal.seed_node(internal.native, "RegulatoryInstrument", "SOP-1.0", internal_props)
    internal.seed_node(internal.baseline, "Policy", "pol_1", {"title": "Encryption Policy"})
    internal_embedder = _Embedder(internal, gateway.system_audit)
    run_export(
        ExportRequest.model_validate({"instrument_id": "SOP-1.0"}),
        config=_config(),
        actor="127.0.0.1",
        dependencies=_dependencies(internal, call_embedding=internal_embedder),
        emitter=emitter,
    )
    assert internal_embedder.calls == ["Encryption Policy"]
    assert internal.embedding_of("Policy", "pol_1") is not None
    assert [(e.name, e.identity) for e in internal.baseline_groups()[0].entries] == [
        ("Policy", "pol_1")
    ]


# --- failure semantics (error mapping) ----------------------------------------------------


def test_a_gateway_refusal_is_an_embedding_stage_failure_and_nothing_is_written(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """The log cannot be written -> 502 at stage `embedding`; no group, no embedding."""
    export_rig.rig.store.fail_appends()
    embedder = _Embedder(export_rig, gateway.system_audit)

    with pytest.raises(ExportStageFailedError) as raised:
        _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert raised.value.stage == "embedding"
    assert all("embedding" not in props for props in export_rig.baseline.nodes.values())
    assert export_rig.rig.store.groups == []


def test_a_group_committed_but_not_applied_is_an_embedding_stage_failure(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """FalkorDB is down after the commit -> 502 at stage `embedding`; the group is in the log."""
    export_rig.baseline.fail_on_write(redis.exceptions.ConnectionError("down"))
    embedder = _Embedder(export_rig, gateway.system_audit)

    with pytest.raises(ExportStageFailedError) as raised:
        _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert raised.value.stage == "embedding"
    assert len(export_rig.baseline_groups()) == 1


def test_a_baseline_behind_its_log_fails_before_any_node_is_read(
    gateway: ProcessGateway, export_rig: ExportRig, emitter: LogEmitter
) -> None:
    """An earlier pending group makes the read stale: fail first, read nothing, embed nothing."""
    from ps_service.graph_gateway.models import MutationGroup, UpsertNode

    export_rig.baseline.fail_on_write(redis.exceptions.ConnectionError("down"))
    pending = gateway.rig.gateway.submit_group(
        MutationGroup(
            graph=export_rig.baseline_name,
            audit_event_id=_audit_id(gateway),
            primitives=(UpsertNode(label="Capability", id="cap_9", properties={"name": "X"}),),
        )
    )
    assert pending.status == "committed_apply_pending"
    export_rig.baseline.heal()
    embedder = _Embedder(export_rig, gateway.system_audit)

    with pytest.raises(ExportStageFailedError) as raised:
        _run(_dependencies(export_rig, call_embedding=embedder), emitter)

    assert raised.value.stage == "embedding"
    assert not any("embedding IS NULL" in q for q in export_rig.baseline_reader().statements)
    assert embedder.calls == []


def _audit_id(gateway: ProcessGateway) -> str:
    return gateway.system_audit(action="x", resource_type="y", resource_id="z", details={})
