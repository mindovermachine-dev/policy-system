"""Tests for `ps_service.api.export_orchestration` (issue #71 PLAN.md S2,
CHANGES.md Appendix A1/A2's merged "new S1" slice).

`run_export` is a thin wrapper calling the injected `ExportStage` delegate
through an `ExportDependencies` bundle (mirrors `RestoreDependencies`). These
tests drive it entirely with fakes -- no real FalkorDB, no real
`ps_service.export.export_instrument` call. The fake FalkorDB double exposes
`list_graphs()` (Appendix A1's safe existence-probe primitive) in addition to
whatever `open_baseline_graph`/`open_native_graph` fake.
"""

from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.api import export_orchestration
from ps_service.api.errors import (
    ExportConfigIncompleteError,
    ExportInstrumentNotFoundError,
    ExportStageFailedError,
)
from ps_service.api.export_orchestration import ExportDependencies, run_export
from ps_service.api.models import ExportRequest
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.export.models import InstrumentManifest
from ps_service.export.serialize import checksum_bytes

if TYPE_CHECKING:
    from collections.abc import Callable

    from company_merge._fakes import MakeEmitter, ReadLines
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.config import ServiceConfig
    from ps_service.export.export_instrument import InstrumentDescriptor

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
_BASELINE_BYTES = b'{"nodes": [], "edges": []}'
_NATIVE_BYTES = b'{"nodes": [], "edges": []}'


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

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        _ = params
        self.call_log.append(q)
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
        call_embedding: object | None = None,
        emitter: object | None = None,
    ) -> InstrumentManifest:
        _ = baseline_graph, native_graph, call_embedding, emitter
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

    dependencies = ExportDependencies(
        open_db=lambda config: cast("FalkorDB", db),
        open_baseline_graph=cast("Callable[[FalkorDB, str], object]", _open_baseline),  # pyright: ignore[reportArgumentType]
        open_native_graph=cast("Callable[[FalkorDB, str], object]", _open_native),  # pyright: ignore[reportArgumentType]
        export=stage,
        call_embedding=None,
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
    # D1's scratch-directory proof: the delegate's own repo_root is never a
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
