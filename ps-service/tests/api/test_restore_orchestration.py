"""Tests for `ps_service.api.restore_orchestration` (D5/D6.3, PLAN.md §0.7).

`run_restoration` is a thin wrapper calling `ps_service.restore.
restore_instrument` through an injected `RestoreDependencies` bundle (mirrors
`PipelineDependencies`). These tests drive it entirely with fakes -- no real
FalkorDB, no real `ps_service.restore` orchestration call.
"""

from __future__ import annotations

import ast
import base64
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.api.errors import RestoreArtifactRejectedError, RestoreStageFailedError
from ps_service.api.models import RestorationManifestPayload, RestorationRequest
from ps_service.api.restore_orchestration import RestoreDependencies, run_restoration
from ps_service.restore.errors import (
    ArtifactContentRejectedError,
    ArtifactIntegrityError,
    ArtifactSchemaVersionMismatchError,
    RestoreConcurrencyConflictError,
)
from ps_service.restore.models import RestoreOutcome

if TYPE_CHECKING:
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.config import ServiceConfig
    from ps_service.restore.models import RestoreArtifact

_MANIFEST_PAYLOAD: dict[str, object] = {
    "instrument_id": "CRA-1.0",
    "celex": "32024R2847",
    "title": "Cyber Resilience Act",
    "short_name": "CRA",
    "version": "1.0",
    "source_type": "external",
    "jurisdiction": "EU",
    "schema_version": "1",
    "exported_at": "2026-01-01T00:00:00Z",
    "baseline_sha256": "a" * 64,
    "native_sha256": "b" * 64,
}


def _valid_request() -> RestorationRequest:
    return RestorationRequest.model_validate(
        {
            "instrument_id": "CRA-1.0",
            "manifest": _MANIFEST_PAYLOAD,
            "baseline_blob_base64": base64.b64encode(b'{"nodes": []}').decode("ascii"),
            "native_blob_base64": base64.b64encode(b'{"nodes": []}').decode("ascii"),
        }
    )


@dataclass
class _FakeDb:
    """A stand-in for `falkordb.FalkorDB` -- never actually touched by these fakes."""


@dataclass
class _RestoreCall:
    artifact: RestoreArtifact
    db: object
    single_tenant_graph_name: str
    similarity_threshold: float
    actor: str


class _FakeRestoreStage:
    def __init__(self, *, error: Exception | None = None, instrument_id: str = "CRA-1.0") -> None:
        self.calls: list[_RestoreCall] = []
        self._error = error
        self._instrument_id = instrument_id

    def __call__(
        self,
        artifact: RestoreArtifact,
        *,
        db: object,
        single_tenant_graph_name: str,
        similarity_threshold: float,
        actor: str,
        emitter: object | None = None,
    ) -> RestoreOutcome:
        _ = emitter
        self.calls.append(
            _RestoreCall(artifact, db, single_tenant_graph_name, similarity_threshold, actor)
        )
        if self._error is not None:
            raise self._error
        return RestoreOutcome(
            instrument_id=self._instrument_id,
            stages=("verified", "staged", "merged_and_finalized"),
        )


def _build_dependencies(stage: _FakeRestoreStage) -> RestoreDependencies:
    return RestoreDependencies(
        open_db=lambda config: cast("FalkorDB", _FakeDb()),
        single_tenant_graph_name=lambda config: "policy_system",
        restore=stage,
    )


def _config() -> ServiceConfig:
    from ps_service.config import ServiceConfig as _ServiceConfig

    return _ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        company_merge_similarity_threshold=0.8,
    )


def test_run_restoration_success_returns_accepted_response_shape() -> None:
    stage = _FakeRestoreStage()
    dependencies = _build_dependencies(stage)

    response = run_restoration(
        _valid_request(), config=_config(), actor="127.0.0.1", dependencies=dependencies
    )

    assert response.instrument_id == "CRA-1.0"
    assert [s.stage for s in response.stages] == ["verified", "staged", "merged_and_finalized"]
    assert len(stage.calls) == 1
    call = stage.calls[0]
    assert call.single_tenant_graph_name == "policy_system"
    assert call.similarity_threshold == 0.8
    assert call.actor == "127.0.0.1"


@pytest.mark.parametrize(
    "delegate_error",
    [
        ArtifactIntegrityError("checksum mismatch"),
        ArtifactSchemaVersionMismatchError("schema mismatch"),
    ],
)
def test_run_restoration_translates_integrity_and_schema_errors_to_rejected(
    delegate_error: Exception,
) -> None:
    stage = _FakeRestoreStage(error=delegate_error)
    dependencies = _build_dependencies(stage)

    with pytest.raises(RestoreArtifactRejectedError):
        run_restoration(_valid_request(), config=_config(), actor="x", dependencies=dependencies)


@pytest.mark.parametrize(
    "delegate_error",
    [
        ArtifactContentRejectedError("label not allow-listed"),
        RestoreConcurrencyConflictError("exhausted retries"),
        RuntimeError("unexpected boom"),
    ],
)
def test_run_restoration_translates_other_errors_to_stage_failed(delegate_error: Exception) -> None:
    stage = _FakeRestoreStage(error=delegate_error)
    dependencies = _build_dependencies(stage)

    with pytest.raises(RestoreStageFailedError) as excinfo:
        run_restoration(_valid_request(), config=_config(), actor="x", dependencies=dependencies)
    assert excinfo.value.stage


def test_run_restoration_rejects_malformed_base64_before_calling_the_delegate() -> None:
    stage = _FakeRestoreStage()
    dependencies = _build_dependencies(stage)
    bad_request = RestorationRequest.model_validate(
        {
            "instrument_id": "CRA-1.0",
            "manifest": _MANIFEST_PAYLOAD,
            "baseline_blob_base64": "not-valid-base64!!!",
            "native_blob_base64": base64.b64encode(b"{}").decode("ascii"),
        }
    )

    with pytest.raises(RestoreArtifactRejectedError):
        run_restoration(bad_request, config=_config(), actor="x", dependencies=dependencies)
    assert stage.calls == []


def test_run_restoration_raises_stage_failed_when_similarity_threshold_unset() -> None:
    from ps_service.config import ServiceConfig

    stage = _FakeRestoreStage()
    dependencies = _build_dependencies(stage)
    config = ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        company_merge_similarity_threshold=None,
    )

    with pytest.raises(RestoreStageFailedError):
        run_restoration(_valid_request(), config=config, actor="x", dependencies=dependencies)
    assert stage.calls == []


def test_manifest_payload_converts_to_instrument_manifest_field_for_field() -> None:
    payload = RestorationManifestPayload.model_validate(_MANIFEST_PAYLOAD)
    stage = _FakeRestoreStage()
    dependencies = _build_dependencies(stage)

    run_restoration(
        RestorationRequest.model_validate(
            {
                "instrument_id": "CRA-1.0",
                "manifest": _MANIFEST_PAYLOAD,
                "baseline_blob_base64": base64.b64encode(b"{}").decode("ascii"),
                "native_blob_base64": base64.b64encode(b"{}").decode("ascii"),
            }
        ),
        config=_config(),
        actor="x",
        dependencies=dependencies,
    )

    manifest = stage.calls[0].artifact.manifest
    assert manifest.instrument_id == payload.instrument_id
    assert manifest.celex == payload.celex
    assert manifest.schema_version == payload.schema_version
    assert manifest.baseline_sha256 == payload.baseline_sha256


def test_main_never_statically_imports_restore_or_company_merge_at_module_load() -> None:
    """M6 guarantee (mirrors `test_main.py`'s existing AST-scan proof).

    `ps_service.main`'s own source must never statically import
    `ps_service.restore` or `ps_service.company_merge` -- both are pulled in
    only function-locally, inside `build_default_restore_dependencies`, at
    request time.
    """
    import ps_service.main as main_module

    forbidden_prefixes = ("ps_service.restore", "ps_service.company_merge")
    source = inspect.getsource(main_module)
    tree = ast.parse(source)

    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_names.append(node.module)
            imported_names.extend(f"{node.module}.{alias.name}" for alias in node.names)

    for name in imported_names:
        assert not name.startswith(forbidden_prefixes), f"forbidden import found: {name}"


def test_restore_orchestration_module_only_imports_restore_instrument_function_locally() -> None:
    """`api/restore_orchestration.py` itself must not statically import
    `ps_service.restore.restore_instrument` (the one submodule that
    transitively pulls in Company Merge) at module level -- only
    `build_default_restore_dependencies` may, function-locally, mirroring
    `ingestion_orchestration.build_default_pipeline_dependencies`'s exact
    pattern.
    """
    import ps_service.api.restore_orchestration as module

    source = inspect.getsource(module)
    tree = ast.parse(source)

    top_level_imports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            top_level_imports.append(node.module)
            top_level_imports.extend(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            top_level_imports.extend(alias.name for alias in node.names)

    assert not any(
        name.startswith("ps_service.restore.restore_instrument") for name in top_level_imports
    ), "ps_service.restore.restore_instrument must only be imported function-locally"
