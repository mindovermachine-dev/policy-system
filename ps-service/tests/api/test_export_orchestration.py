"""Tests for `ps_service.api.export_orchestration`.

`run_export` is a thin wrapper calling the injected `ExportStage` delegate
through an `ExportDependencies` bundle (mirrors `RestoreDependencies`). These
tests drive it entirely with fakes -- no real FalkorDB, no real
`ps_service.export.export_instrument` call. The fake FalkorDB double exposes
`list_graphs()` (Appendix A1's safe existence-probe primitive) in addition to
whatever `open_baseline_graph`/`open_native_graph` fake.
"""

from __future__ import annotations

import base64
import dataclasses
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import redis.exceptions
from company_merge._kinds import UNREGISTERED_KIND
from export._gateway_fakes import ScriptedPolicyGraph, pending_review

from api._gateway_fakes import installed_process_gateway, process_gateway_installed
from ps_service.api import export_orchestration
from ps_service.api.errors import (
    ExportConfigIncompleteError,
    ExportInstrumentNotFoundError,
    ExportStageFailedError,
    ExportUnresolvedNearMissesError,
    ReviewKindUnsupportedError,
)
from ps_service.api.export_orchestration import ExportDependencies, run_export
from ps_service.api.graph_write_access import graph_write_access
from ps_service.api.models import ExportRequest
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.export.models import InstrumentManifest
from ps_service.export.serialize import checksum_bytes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from company_merge._fakes import MakeEmitter, ReadLines
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.api.models import ExportAcceptedResponse
    from ps_service.company_merge.models import PendingReviewRecord
    from ps_service.company_merge.reviewable_kinds import ReviewableKind
    from ps_service.config import ServiceConfig
    from ps_service.export.export_instrument import InstrumentDescriptor
    from ps_service.logging import LogEmitter

_INSTRUMENT_ID = "CRA-1.0"
_SHORT_NAME = "CRA"
_BASELINE_NAME = baseline_graph_name(_SHORT_NAME)
_PROPERTIES: dict[str, object] = {
    "id": _INSTRUMENT_ID,
    "title": "Cyber Resilience Act",
    "source_type": "external",
    "jurisdiction": "EU",
    "version": "1.0",
    "celex": "32024R2847",
}
_NODE_IDS = re.compile(r"^MATCH \(n:(\w+)\) RETURN n\.id$")
_BASELINE_BYTES = b'{"nodes": [], "edges": []}'
_NATIVE_BYTES = b'{"nodes": [], "edges": []}'


@pytest.fixture(autouse=True)
def _process_gateway() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture
    """The gateway and system audit recorder the app lifespan installs."""
    with process_gateway_installed():
        yield


def _request() -> ExportRequest:
    return ExportRequest.model_validate({"instrument_id": _INSTRUMENT_ID})


@dataclass
class _FakeDb:
    """A stand-in for `falkordb.FalkorDB` exposing only `list_graphs()`."""

    graphs: list[str] = field(default_factory=list)

    def list_graphs(self) -> list[str]:
        return self.graphs


class _FakeQueryResult:
    def __init__(self, result_set: list[list[object]]) -> None:
        self.result_set = result_set


@dataclass
class _FakeExistenceGraph:
    """Answers only the one query `run_export` issues against the baseline graph."""

    properties: dict[str, object] | None
    call_log: list[str] = field(default_factory=list)
    capability_ids: tuple[str, ...] = ()
    ids_by_label: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Ids of the nodes of any other label, for the per-kind id read of the near-miss gate."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        _ = params
        self.call_log.append(q)
        if id_read := _NODE_IDS.fullmatch(q):
            label = id_read[1]
            ids = self.capability_ids if label == "Capability" else self.ids_by_label.get(label, ())
            return _FakeQueryResult([[node_id] for node_id in ids])
        assert q == "MATCH (n:RegulatoryInstrument {id: $id}) RETURN properties(n)"
        if self.properties is None:
            return _FakeQueryResult([])
        return _FakeQueryResult([[dict(self.properties)]])


@dataclass
class _ExportCall:
    descriptor: InstrumentDescriptor
    repo_root: Path
    packaged_copy_path: Path
    embed_model: str


class _FakeExportStage:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[_ExportCall] = []
        self._error = error

    def __call__(
        self,
        descriptor: InstrumentDescriptor,
        *,
        baseline_graph: object,
        native_graph: object,
        embed_model: str,
        repo_root: Path,
        packaged_copy_path: Path,
        submitter: object,
        audit_event_id: str,
        baseline_graph_name: str,
        call_embedding: object | None = None,
        emitter: object | None = None,
    ) -> InstrumentManifest:
        _ = baseline_graph, native_graph, call_embedding, emitter
        _ = submitter, audit_event_id, baseline_graph_name
        self.calls.append(_ExportCall(descriptor, repo_root, packaged_copy_path, embed_model))
        if self._error is not None:
            raise self._error
        instrument_dir = repo_root / "curated-content" / descriptor.instrument_id
        instrument_dir.mkdir(parents=True, exist_ok=True)
        (instrument_dir / "baseline.json").write_bytes(_BASELINE_BYTES)
        (instrument_dir / "native.json").write_bytes(_NATIVE_BYTES)
        return InstrumentManifest(
            instrument_id=descriptor.instrument_id,
            celex=descriptor.celex,
            title=descriptor.title,
            short_name=descriptor.short_name,
            version=descriptor.version,
            source_type=descriptor.source_type,
            jurisdiction=descriptor.jurisdiction,
            schema_version=DOMAIN_SCHEMA_VERSION,
            exported_at="2026-01-01T00:00:00Z",
            baseline_sha256=checksum_bytes(_BASELINE_BYTES),
            native_sha256=checksum_bytes(_NATIVE_BYTES),
        )


def _build_dependencies(
    *,
    db: _FakeDb,
    baseline_graph: _FakeExistenceGraph,
    stage: _FakeExportStage,
    policy_graph: ScriptedPolicyGraph | None = None,
    opened_policy_graphs: list[str] | None = None,
) -> tuple[ExportDependencies, list[str], list[str]]:
    open_baseline_calls: list[str] = []
    open_native_calls: list[str] = []

    def _open_baseline(db_arg: FalkorDB, short_name: str) -> _FakeExistenceGraph:
        _ = db_arg
        open_baseline_calls.append(short_name)
        return baseline_graph

    def _open_native(db_arg: FalkorDB, short_name: str) -> object:
        _ = db_arg
        open_native_calls.append(short_name)
        return object()

    policy = policy_graph if policy_graph is not None else ScriptedPolicyGraph()

    def _open_policy(db_arg: object, graph_name: str) -> ScriptedPolicyGraph:
        _ = db_arg
        if opened_policy_graphs is not None:
            opened_policy_graphs.append(graph_name)
        return policy

    dependencies = ExportDependencies(
        open_db=lambda config: cast("FalkorDB", db),
        open_baseline_graph=cast("Callable[[FalkorDB, str], object]", _open_baseline),  # pyright: ignore[reportArgumentType]
        open_native_graph=cast("Callable[[FalkorDB, str], object]", _open_native),  # pyright: ignore[reportArgumentType]
        export=stage,
        call_embedding=None,
        graph_write_access=graph_write_access,
        open_single_tenant_graph=_open_policy,
    )
    return dependencies, open_baseline_calls, open_native_calls


def _config(*, embed_model: str | None = "azure/text-embedding-3-large") -> ServiceConfig:
    from ps_service.config import ServiceConfig as _ServiceConfig

    return _ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_embed_model=embed_model,
    )


# --- case 1: success -----------------------------------------------------------


def test_run_export_success_returns_accepted_response_shape(make_emitter: MakeEmitter) -> None:
    emitter, _log_path = make_emitter()
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage()
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    response = run_export(
        _request(), config=_config(), actor="127.0.0.1", dependencies=dependencies, emitter=emitter
    )

    assert response.instrument_id == _INSTRUMENT_ID
    assert base64.b64decode(response.baseline_blob_base64) == _BASELINE_BYTES
    assert base64.b64decode(response.native_blob_base64) == _NATIVE_BYTES
    assert response.manifest.instrument_id == _INSTRUMENT_ID
    assert response.manifest.title == "Cyber Resilience Act"
    assert response.manifest.celex == "32024R2847"
    assert [s.stage for s in response.stages] == ["embedded", "serialized", "cataloged"]
    assert len(stage.calls) == 1
    # Scratch-directory proof: the delegate's own repo_root is never a
    # real repo path -- it is rooted under the OS temp directory.
    call = stage.calls[0]
    assert str(call.repo_root).startswith(tempfile.gettempdir())
    assert str(call.packaged_copy_path).startswith(tempfile.gettempdir())


# --- case 2a: graph key never existed --------------------------------------


def test_run_export_raises_not_found_when_baseline_graph_key_never_existed() -> None:
    db = _FakeDb(graphs=[])  # derived baseline name absent
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage()
    dependencies, open_baseline_calls, open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    with pytest.raises(ExportInstrumentNotFoundError, match=_INSTRUMENT_ID):
        run_export(_request(), config=_config(), actor="x", dependencies=dependencies)

    assert open_baseline_calls == []
    assert open_native_calls == []
    assert stage.calls == []
    assert baseline_graph.call_log == []


# --- case 2b: graph key exists, node missing -------------------------------


def test_run_export_raises_not_found_when_graph_exists_but_node_missing() -> None:
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=None)  # empty result set
    stage = _FakeExportStage()
    dependencies, open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    with pytest.raises(ExportInstrumentNotFoundError):
        run_export(_request(), config=_config(), actor="x", dependencies=dependencies)

    assert open_baseline_calls == [_SHORT_NAME]
    assert baseline_graph.call_log  # the existence MATCH was issued
    assert stage.calls == []


# --- case 3: malformed id ----------------------------------------------------


def test_run_export_raises_not_found_for_malformed_id_with_zero_graph_access() -> None:
    db = _FakeDb(graphs=[])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage()
    dependencies, open_baseline_calls, open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )
    request = ExportRequest.model_validate({"instrument_id": "nohyphen"})

    with pytest.raises(ExportInstrumentNotFoundError):
        run_export(request, config=_config(), actor="x", dependencies=dependencies)

    assert open_baseline_calls == []
    assert open_native_calls == []
    assert stage.calls == []


# --- case 4: config incomplete ------------------------------------------------


def test_run_export_raises_config_incomplete_when_embed_model_unset() -> None:
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage()
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    with pytest.raises(ExportConfigIncompleteError):
        run_export(
            _request(), config=_config(embed_model=None), actor="x", dependencies=dependencies
        )

    assert stage.calls == []


# --- case 5: delegate raises ---------------------------------------------------


def test_run_export_translates_unexpected_delegate_failure_to_stage_failed(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _log_path = make_emitter()
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage(error=RuntimeError("boom"))
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    with pytest.raises(ExportStageFailedError) as excinfo:
        run_export(
            _request(), config=_config(), actor="x", dependencies=dependencies, emitter=emitter
        )
    assert excinfo.value.stage


# --- case 6: delegate raises ExportSourceGraphError -> "serialization" -------


def test_run_export_classifies_source_graph_error_as_serialization_stage(
    make_emitter: MakeEmitter,
) -> None:
    from ps_service.export.errors import ExportSourceGraphError

    emitter, _log_path = make_emitter()
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage(error=ExportSourceGraphError("node with two labels"))
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    with pytest.raises(ExportStageFailedError) as excinfo:
        run_export(
            _request(), config=_config(), actor="x", dependencies=dependencies, emitter=emitter
        )
    assert excinfo.value.stage == "serialization"


# --- case 7: audit log success -------------------------------------------------


def test_run_export_success_emits_started_and_succeeded_audit_log_entries(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage()
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    run_export(
        _request(), config=_config(), actor="test-actor", dependencies=dependencies, emitter=emitter
    )
    emitter.flush()

    entries = [
        row
        for row in read_lines(log_path)
        if row["component"] == "export" and row["action"] == "export_instrument"
    ]
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "succeeded"]
    for entry in entries:
        assert entry["entity_id"] == _INSTRUMENT_ID
        assert entry["caller"] == "test-actor"
        assert "actor" not in entry
    assert entries[0]["schema_version"] == DOMAIN_SCHEMA_VERSION
    assert entries[1]["schema_version"] == DOMAIN_SCHEMA_VERSION


# --- case 8: audit log failure -------------------------------------------------


def test_run_export_failure_emits_started_and_failed_audit_log_entries_only(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage(error=RuntimeError("boom"))
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    with pytest.raises(ExportStageFailedError):
        run_export(
            _request(),
            config=_config(),
            actor="test-actor",
            dependencies=dependencies,
            emitter=emitter,
        )
    emitter.flush()

    entries = [
        row
        for row in read_lines(log_path)
        if row["component"] == "export" and row["action"] == "export_instrument"
    ]
    outcomes = [entry["outcome"] for entry in entries]
    assert outcomes == ["started", "failed"]
    assert "succeeded" not in outcomes


# --- case 9: scratch directory always cleaned up -------------------------------


def test_run_export_always_removes_the_scratch_directory(
    monkeypatch: pytest.MonkeyPatch, make_emitter: MakeEmitter
) -> None:
    emitter, _log_path = make_emitter()
    recorded_paths: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _recording_mkdtemp(prefix: str | None = None) -> str:
        path = real_mkdtemp(prefix=prefix)
        recorded_paths.append(path)
        return path

    monkeypatch.setattr(export_orchestration.tempfile, "mkdtemp", _recording_mkdtemp)

    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES)
    stage = _FakeExportStage()
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage
    )

    run_export(_request(), config=_config(), actor="x", dependencies=dependencies, emitter=emitter)

    assert len(recorded_paths) == 1
    assert not Path(recorded_paths[0]).exists()

    # And again on the failure path.
    failing_stage = _FakeExportStage(error=RuntimeError("boom"))
    failing_dependencies, _open_baseline_calls2, _open_native_calls2 = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=failing_stage
    )
    with pytest.raises(ExportStageFailedError):
        run_export(
            _request(),
            config=_config(),
            actor="x",
            dependencies=failing_dependencies,
            emitter=emitter,
        )

    assert len(recorded_paths) == 2
    assert not Path(recorded_paths[1]).exists()


# --- near-miss gate: pass-through and other-instrument reviews (PIN) ------------


def _run_with_reviews(
    reviews: list[PendingReviewRecord],
    emitter: LogEmitter,
    *,
    capability_ids: tuple[str, ...] = ("cap_a", "cap_b"),
) -> tuple[_FakeExportStage, ExportAcceptedResponse]:
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES, capability_ids=capability_ids)
    stage = _FakeExportStage()
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage, policy_graph=ScriptedPolicyGraph(reviews)
    )
    response = run_export(
        _request(), config=_config(), actor="x", dependencies=dependencies, emitter=emitter
    )
    return stage, response


def test_the_near_miss_gate_opens_the_configured_single_tenant_graph(
    make_emitter: MakeEmitter,
) -> None:
    """The gate reads the graph named by `config.falkordb_graph`."""
    opened: list[str] = []
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=_FakeDb(graphs=[_BASELINE_NAME]),
        baseline_graph=_FakeExistenceGraph(properties=_PROPERTIES),
        stage=_FakeExportStage(),
        opened_policy_graphs=opened,
    )

    run_export(
        _request(),
        config=dataclasses.replace(_config(), falkordb_graph="custom_graph"),
        actor="x",
        dependencies=dependencies,
        emitter=make_emitter()[0],
    )

    assert opened == ["custom_graph"]


def test_run_export_runs_when_policy_system_holds_no_reviews(make_emitter: MakeEmitter) -> None:
    """No review at all, the export runs exactly as before."""
    stage, response = _run_with_reviews([], make_emitter()[0])

    assert len(stage.calls) == 1
    assert response.instrument_id == _INSTRUMENT_ID


def test_run_export_is_not_refused_by_reviews_of_other_instruments(
    make_emitter: MakeEmitter,
) -> None:
    """Reviews naming only Capability ids absent from this baseline do not block."""
    stage, response = _run_with_reviews(
        [pending_review("review_other", incoming_id="cap_x", nearest_existing_id="cap_y")],
        make_emitter()[0],
    )

    assert len(stage.calls) == 1
    assert response.instrument_id == _INSTRUMENT_ID


def test_run_export_is_refused_when_only_one_side_of_a_review_is_in_the_instrument(
    make_emitter: MakeEmitter,
) -> None:
    """A review with one instrument side and one foreign side still blocks."""
    with pytest.raises(ExportUnresolvedNearMissesError) as raised:
        _run_with_reviews(
            [pending_review("review_mixed", incoming_id="cap_x", nearest_existing_id="cap_b")],
            make_emitter()[0],
        )

    assert raised.value.review_ids == ("review_mixed",)
    assert raised.value.count == 1


# --- a refused export writes nothing and logs one `refused` entry ---------------


def test_refused_export_writes_nothing_and_emits_exactly_one_refused_entry(
    monkeypatch: pytest.MonkeyPatch, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """A refused export leaves no audit row, backfill, scratch dir or `started` entry."""
    emitter, log_path = make_emitter()
    recorded_paths: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _recording_mkdtemp(prefix: str | None = None) -> str:
        path = real_mkdtemp(prefix=prefix)
        recorded_paths.append(path)
        return path

    monkeypatch.setattr(export_orchestration.tempfile, "mkdtemp", _recording_mkdtemp)
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES, capability_ids=("cap_a", "cap_b"))
    stage = _FakeExportStage()
    policy = ScriptedPolicyGraph(
        [
            pending_review("review_1", incoming_id="cap_a"),
            pending_review("review_2", nearest_existing_id="cap_b"),
        ]
    )
    dependencies, _open_baseline_calls, open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage, policy_graph=policy
    )

    with pytest.raises(ExportUnresolvedNearMissesError):
        run_export(
            _request(),
            config=_config(),
            actor="test-actor",
            dependencies=dependencies,
            emitter=emitter,
        )
    emitter.flush()

    installed = installed_process_gateway()
    assert installed.system_audit.rows == []
    assert installed.rig.events == []  # no embedding backfill group reached the gateway
    assert stage.calls == []
    assert open_native_calls == []
    assert recorded_paths == []
    entries = [
        row
        for row in read_lines(log_path)
        if row["component"] == "export" and row["action"] == "export_instrument"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["outcome"] == "refused"
    assert entry["entity_id"] == _INSTRUMENT_ID
    assert entry["caller"] == "test-actor"
    assert entry["schema_version"] == DOMAIN_SCHEMA_VERSION
    assert entry["review_count"] == 2
    assert "review_1" not in str(entry)


def test_export_is_refused_naming_a_review_of_the_test_only_kind(
    make_emitter: MakeEmitter, test_only_reviewable_kind: ReviewableKind
) -> None:
    """A review of a registered kind that is not Capability blocks the export like any other."""
    emitter, _log_path = make_emitter()
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(
        properties=_PROPERTIES, ids_by_label={test_only_reviewable_kind.label: ("probe_a",)}
    )
    stage = _FakeExportStage()
    policy = ScriptedPolicyGraph(
        [
            pending_review(
                "review_probe", kind=test_only_reviewable_kind.label, incoming_id="probe_a"
            )
        ]
    )
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage, policy_graph=policy
    )

    with pytest.raises(ExportUnresolvedNearMissesError) as raised:
        run_export(
            _request(), config=_config(), actor="x", dependencies=dependencies, emitter=emitter
        )

    assert raised.value.review_ids == ("review_probe",)
    installed = installed_process_gateway()
    assert installed.system_audit.rows == []
    assert installed.rig.events == []
    assert stage.calls == []


def test_export_is_refused_by_an_unresolved_role_review_and_writes_nothing(
    make_emitter: MakeEmitter,
) -> None:
    """A Role near miss over this baseline's Roles blocks the export like a Capability one."""
    emitter, _log_path = make_emitter()
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(
        properties=_PROPERTIES, ids_by_label={"Role": ("role_a", "role_b")}
    )
    stage = _FakeExportStage()
    policy = ScriptedPolicyGraph([pending_review("review_role", kind="Role", incoming_id="role_a")])
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage, policy_graph=policy
    )

    with pytest.raises(ExportUnresolvedNearMissesError) as raised:
        run_export(
            _request(), config=_config(), actor="x", dependencies=dependencies, emitter=emitter
        )

    assert raised.value.review_ids == ("review_role",)
    installed = installed_process_gateway()
    assert installed.system_audit.rows == []
    assert installed.rig.events == []
    assert stage.calls == []


def test_export_with_a_review_of_an_unregistered_kind_is_refused_naming_it_and_writes_nothing(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES, capability_ids=("cap_a",))
    stage = _FakeExportStage()
    policy = ScriptedPolicyGraph([pending_review("review_stray", kind=UNREGISTERED_KIND)])
    dependencies, _open_baseline_calls, open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage, policy_graph=policy
    )

    with pytest.raises(ReviewKindUnsupportedError, match=UNREGISTERED_KIND):
        run_export(
            _request(), config=_config(), actor="x", dependencies=dependencies, emitter=emitter
        )
    emitter.flush()

    installed = installed_process_gateway()
    assert installed.system_audit.rows == []
    assert installed.rig.events == []
    assert stage.calls == []
    assert open_native_calls == []
    entries = [
        row
        for row in read_lines(log_path)
        if row["component"] == "export" and row["action"] == "export_instrument"
    ]
    assert [(row["outcome"], row["entity_id"]) for row in entries] == [("refused", _INSTRUMENT_ID)]
    assert "review_count" not in entries[0]


# --- fail closed when `policy_system` cannot be read (PIN) ----------------------


def test_unreadable_policy_system_fails_closed_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch, make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """The read error propagates; no audit row, backfill, scratch dir or log entry."""
    emitter, log_path = make_emitter()
    recorded_paths: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _recording_mkdtemp(prefix: str | None = None) -> str:
        path = real_mkdtemp(prefix=prefix)
        recorded_paths.append(path)
        return path

    monkeypatch.setattr(export_orchestration.tempfile, "mkdtemp", _recording_mkdtemp)
    db = _FakeDb(graphs=[_BASELINE_NAME])
    baseline_graph = _FakeExistenceGraph(properties=_PROPERTIES, capability_ids=("cap_a",))
    stage = _FakeExportStage()
    policy = ScriptedPolicyGraph(fail_with=redis.exceptions.ConnectionError("policy_system down"))
    dependencies, _open_baseline_calls, _open_native_calls = _build_dependencies(
        db=db, baseline_graph=baseline_graph, stage=stage, policy_graph=policy
    )

    with pytest.raises(redis.exceptions.ConnectionError, match="policy_system down"):
        run_export(
            _request(), config=_config(), actor="x", dependencies=dependencies, emitter=emitter
        )
    emitter.flush()

    installed = installed_process_gateway()
    assert installed.system_audit.rows == []
    assert installed.rig.events == []
    assert stage.calls == []
    assert recorded_paths == []
    assert [row for row in read_lines(log_path) if row["component"] == "export"] == []
