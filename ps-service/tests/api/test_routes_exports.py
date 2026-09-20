"""HTTP tests for `POST /exports` (issue #71, PLAN.md S3 / CHANGES.md
Appendix A2's "new S2").

Mirrors `test_routes_restorations.py`'s style exactly: `TestClient` +
`app.dependency_overrides` supplying a fake `ExportDependencies` bundle, so
request validation, the export hand-off, and error mapping are exercised
without a real graph or LLM.
"""

from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from ps_service.api import export_orchestration
from ps_service.api.dependencies import provide_export_dependencies
from ps_service.api.export_orchestration import ExportDependencies, ExportStage
from ps_service.config import ServiceConfig
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import InstrumentManifest
from ps_service.export.serialize import checksum_bytes
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from api._fakes import ReadLines
    from ps_service.export.export_instrument import InstrumentDescriptor

_INSTRUMENT_ID = "CRA-1.0"
_BASELINE_BYTES = b'{"nodes": [], "edges": []}'
_NATIVE_BYTES = b'{"nodes": [], "edges": []}'


def _valid_body() -> dict[str, object]:
    return {"instrument_id": _INSTRUMENT_ID}


@dataclass
class _FakeDb:
    """Never actually touched except via `list_graphs()` (Appendix A1's safe probe)."""

    graphs: list[str] = field(default_factory=list)

    def list_graphs(self) -> list[str]:
        return self.graphs


class _FakeQueryResult:
    def __init__(self, result_set: list[list[object]]) -> None:
        self.result_set = result_set


class _FakeExistenceGraph:
    """Answers the one existence `MATCH` `run_export` issues, when reached."""

    def query(self, q: str, params: dict[str, object] | None = None) -> _FakeQueryResult:
        _ = q, params
        return _FakeQueryResult(
            [
                [
                    {
                        "id": _INSTRUMENT_ID,
                        "title": "Cyber Resilience Act",
                        "source_type": "external",
                        "jurisdiction": "EU",
                        "version": "1.0",
                        "celex": "32024R2847",
                    }
                ]
            ]
        )


class _FakeExportStage:
    """A stand-in for `export_instrument()` that always writes deterministic blobs."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.call_count = 0
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
        _ = baseline_graph, native_graph, embed_model, packaged_copy_path, call_embedding, emitter
        self.call_count += 1
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


class _PartialWriteExportStage:
    """Writes `baseline.json` successfully, then raises before `native.json` (S11 item 1)."""

    def __init__(self) -> None:
        self.call_count = 0

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
        _ = baseline_graph, native_graph, embed_model, packaged_copy_path, call_embedding, emitter
        self.call_count += 1
        instrument_dir = repo_root / "curated-content" / descriptor.instrument_id
        instrument_dir.mkdir(parents=True, exist_ok=True)
        (instrument_dir / "baseline.json").write_bytes(_BASELINE_BYTES)
        raise RuntimeError("native serialization boom (mid-orchestration failure)")


def _open_baseline(db_arg: FalkorDB, short_name: str) -> _FakeExistenceGraph:
    _ = db_arg, short_name
    return _FakeExistenceGraph()


def _open_native(db_arg: FalkorDB, short_name: str) -> object:
    _ = db_arg, short_name
    return object()


def _fake_dependencies(
    stage: ExportStage, *, graphs: list[str] | None = None
) -> ExportDependencies:
    db = _FakeDb(graphs=graphs if graphs is not None else ["cra_baseline"])
    return ExportDependencies(
        open_db=lambda config: cast("FalkorDB", db),
        open_baseline_graph=cast("Callable[[FalkorDB, str], object]", _open_baseline),  # pyright: ignore[reportArgumentType]
        open_native_graph=cast("Callable[[FalkorDB, str], object]", _open_native),  # pyright: ignore[reportArgumentType]
        export=stage,
        call_embedding=None,
    )


def _app_config(*, embed_model: str | None = "azure/text-embedding-3-large") -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        llm_interface_embed_model=embed_model,
        is_local_test_bypass_active=True,
    )


def _noop_emit(**_kwargs: object) -> None:
    """Discard an audit-log entry (Logging boundary stub -- see `_stub_export_log`)."""


@pytest.fixture
def _stub_export_log(  # pyright: ignore[reportUnusedFunction] -- requested via usefixtures, not autouse
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the Logging boundary so `run_export` needs no `configure()`d facade.

    Mirrors `test_routes_change_checks.py`'s own `_stub_run_log` fixture
    exactly, for the identical reason: `create_export` passes no explicit
    emitter, so `run_export`'s `_emit_export_log` (D4) emits through the
    process-wide default emitter, which these fast HTTP tests deliberately do
    not `configure()`. Not autouse because
    `test_actor_is_threaded_from_caller_host` needs the real facade instead;
    every other test requests this explicitly via
    `@pytest.mark.usefixtures("_stub_export_log")`.
    """
    monkeypatch.setattr("ps_service.api.export_orchestration.emit_log_entry", _noop_emit)


def _client_with_fake(
    stage: ExportStage,
    *,
    graphs: list[str] | None = None,
    config: ServiceConfig | None = None,
) -> TestClient:
    app = create_app(config or _app_config())
    app.dependency_overrides[provide_export_dependencies] = lambda: _fake_dependencies(
        stage, graphs=graphs
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.usefixtures("_stub_export_log")
def test_valid_export_body_returns_200_with_expected_shape() -> None:
    stage = _FakeExportStage()
    client = _client_with_fake(stage)

    response = client.post("/exports", json=_valid_body())

    assert response.status_code == 200
    body = response.json()
    assert body["instrument_id"] == _INSTRUMENT_ID
    assert body["manifest"]["source_type"] == "external"
    assert base64.b64decode(body["baseline_blob_base64"]) == _BASELINE_BYTES
    assert base64.b64decode(body["native_blob_base64"]) == _NATIVE_BYTES
    assert [s["stage"] for s in body["stages"]] == ["embedded", "serialized", "cataloged"]
    assert stage.call_count == 1


@pytest.mark.usefixtures("_stub_export_log")
def test_not_found_instrument_id_returns_404() -> None:
    stage = _FakeExportStage()
    client = _client_with_fake(stage, graphs=[])  # derived baseline key absent

    response = client.post("/exports", json=_valid_body())

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "export_instrument_not_found"
    assert stage.call_count == 0


@pytest.mark.usefixtures("_stub_export_log")
def test_config_incomplete_returns_503() -> None:
    stage = _FakeExportStage()
    client = _client_with_fake(stage, config=_app_config(embed_model=None))

    response = client.post("/exports", json=_valid_body())

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "export_config_incomplete"
    assert stage.call_count == 0


@pytest.mark.usefixtures("_stub_export_log")
def test_stage_failure_returns_502_naming_the_stage() -> None:
    stage = _FakeExportStage(error=RuntimeError("unexpected boom"))
    client = _client_with_fake(stage)

    response = client.post("/exports", json=_valid_body())

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "export_stage_failed"
    assert body["error"]["failing_stage"]


def _recording_mkdtemp(monkeypatch: pytest.MonkeyPatch, recorded_paths: list[str]) -> None:
    """Monkeypatch `export_orchestration.tempfile.mkdtemp` to capture the scratch path it mints."""
    real_mkdtemp = tempfile.mkdtemp

    def _recorder(prefix: str | None = None) -> str:
        path = real_mkdtemp(prefix=prefix)
        recorded_paths.append(path)
        return path

    monkeypatch.setattr(export_orchestration.tempfile, "mkdtemp", _recorder)


@pytest.mark.usefixtures("_stub_export_log")
def test_partial_write_failure_returns_502_and_leaves_no_residual_scratch_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-011 route-level proof (PLAN.md S11 item 1): a delegate that writes
    `baseline.json` then raises before `native.json` never leaves a residual
    scratch directory reachable after the request completes -- no partial
    artifact survives anywhere, matching AC-BI-011's "no partial/corrupt
    artifact is returned" phrasing.
    """
    recorded_paths: list[str] = []
    _recording_mkdtemp(monkeypatch, recorded_paths)

    stage = _PartialWriteExportStage()
    client = _client_with_fake(stage)

    response = client.post("/exports", json=_valid_body())

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "export_stage_failed"
    assert stage.call_count == 1
    assert len(recorded_paths) == 1
    assert not Path(recorded_paths[0]).exists()


@pytest.mark.usefixtures("_stub_export_log")
def test_immediate_stage_failure_returns_502_and_leaves_no_residual_scratch_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-011 route-level proof (PLAN.md S11 item 2): a delegate raising
    immediately (before any write) still leaves no residual scratch
    directory, and the named failing stage is the delegate's own -- not a
    guard-clause default (`"configuration"`/`"validation"`).
    """
    recorded_paths: list[str] = []
    _recording_mkdtemp(monkeypatch, recorded_paths)

    stage = _FakeExportStage(error=RuntimeError("boom before any write"))
    client = _client_with_fake(stage)

    response = client.post("/exports", json=_valid_body())

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "export_stage_failed"
    failing_stage = body["error"]["failing_stage"]
    assert failing_stage
    assert failing_stage not in ("configuration", "validation")
    assert len(recorded_paths) == 1
    assert not Path(recorded_paths[0]).exists()


@pytest.mark.usefixtures("_stub_export_log")
def test_stage_failure_message_never_leaks_the_scratch_path() -> None:
    """AC-BI-012 security proof (PLAN.md S12): a delegate exception whose own
    message embeds a realistic absolute scratch path must never surface that
    path in the response body -- a generic, scrubbed message is present
    instead.
    """
    leaking_message = (
        "[Errno 2] No such file or directory: "
        "'/tmp/ps-export-a1b2c3/curated-content/CRA-1.0/native.json'"
    )
    stage = _FakeExportStage(error=FileNotFoundError(leaking_message))
    client = _client_with_fake(stage)

    response = client.post("/exports", json=_valid_body())

    assert response.status_code == 502
    body = response.json()
    message = body["error"]["message"]
    assert "/tmp/ps-export" not in message  # noqa: S108 - assertion text, not a path this test opens
    assert message
    assert message != leaking_message


@pytest.mark.usefixtures("_stub_export_log")
def test_malformed_body_returns_422_and_never_calls_the_delegate() -> None:
    stage = _FakeExportStage()
    client = _client_with_fake(stage)

    response = client.post("/exports", json={})

    assert response.status_code == 422
    assert stage.call_count == 0


@pytest.mark.parametrize("path", ["/exports"])
def test_exports_route_is_unauthenticated_never_401_or_403(path: str) -> None:
    stage = _FakeExportStage()
    client = _client_with_fake(stage)

    response = client.post(path, json=_valid_body())

    assert response.status_code not in (401, 403)


def test_actor_is_threaded_from_caller_host(
    configured_logging: Path, read_lines: ReadLines
) -> None:
    """`create_export` passes no explicit emitter, so `run_export`'s
    `_emit_export_log` entries flow through the process-default emitter
    `configured_logging` installs (mirrors
    `test_routes_change_checks.py`'s
    `test_post_change_checks_response_run_id_matches_the_provide_run_id_binding`
    and `test_run_context.py`'s own established pattern for this exact
    situation) -- proof AC-BI-013's `caller` field is the requesting
    client's host, not a placeholder.
    """
    stage = _FakeExportStage()
    client = _client_with_fake(stage)

    response = client.post("/exports", json=_valid_body())

    assert response.status_code == 200

    facade.reset_for_tests()  # drain + join the writer thread so the file is complete
    lines = read_lines(configured_logging)

    entries = [
        row
        for row in lines
        if row.get("component") == "export" and row.get("action") == "export_instrument"
    ]
    assert entries, "run_export emitted no export_instrument lines"
    assert all(entry.get("caller") == "testclient" for entry in entries)
