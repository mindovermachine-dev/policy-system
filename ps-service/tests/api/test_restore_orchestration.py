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
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from api._audit_fakes import InMemoryAuditStore
from ps_service.api.errors import RestoreArtifactRejectedError, RestoreStageFailedError
from ps_service.api.models import RestorationManifestPayload, RestorationRequest
from ps_service.api.restore_orchestration import (
    _STAGE_REASON_MAX_LEN,  # pyright: ignore[reportPrivateUsage]  # test pins the cap this module applies
    RestoreDependencies,
)
from ps_service.api.restore_orchestration import (
    run_restoration as _run_restoration,
)
from ps_service.audit import AuditContext
from ps_service.restore.errors import (
    ArtifactContentRejectedError,
    ArtifactIntegrityError,
    ArtifactSchemaVersionMismatchError,
    RestoreConcurrencyConflictError,
)
from ps_service.restore.models import RestoreOutcome

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.api.models import RestorationAcceptedResponse
    from ps_service.config import ServiceConfig
    from ps_service.logging import LogEmitter
    from ps_service.restore.models import RestoreArtifact

_REPO_ROOT = Path(__file__).resolve().parents[3]


_AUDIT = AuditContext(("test-actor", "https://issuer.example.com/"), InMemoryAuditStore())


def run_restoration(
    request_body: RestorationRequest,
    *,
    config: ServiceConfig,
    actor: str,
    dependencies: RestoreDependencies,
    owner: tuple[str, str] | None = None,
    emitter: LogEmitter | None = None,
) -> RestorationAcceptedResponse:
    """Run `run_restoration` with an in-memory audit context (these tests are not about audit)."""
    return _run_restoration(
        request_body,
        config=config,
        actor=actor,
        dependencies=dependencies,
        audit=_AUDIT,
        owner=owner,
        emitter=emitter,
    )


_INSTRUMENT_ID_MISMATCH_MESSAGE = (
    "no RegulatoryInstrument node with id 'X-1.0' found in the staged baseline graph -- the "
    "manifest's instrument_id must match the source graph's actual RegulatoryInstrument.id"
)

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
    owner: tuple[str, str] | None = None


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
        owner: tuple[str, str] | None = None,
    ) -> RestoreOutcome:
        _ = emitter
        self.calls.append(
            _RestoreCall(artifact, db, single_tenant_graph_name, similarity_threshold, actor, owner)
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
def test_run_restoration_translates_other_errors_to_stage_failed(
    delegate_error: Exception, configured_logging: Path
) -> None:
    _ = configured_logging
    stage = _FakeRestoreStage(error=delegate_error)
    dependencies = _build_dependencies(stage)

    with pytest.raises(RestoreStageFailedError) as excinfo:
        run_restoration(_valid_request(), config=_config(), actor="x", dependencies=dependencies)
    assert excinfo.value.stage


@pytest.mark.parametrize(
    "message",
    [
        "node label 'PracticeArea' is not in the allow-list ['Capability', 'Control']",
        _INSTRUMENT_ID_MISMATCH_MESSAGE,  # restore_instrument.py's second raise site (id mismatch)
    ],
)
def test_run_restoration_surfaces_whitelisted_content_rejections_verbatim(message: str) -> None:
    """GH #104 / AC-BI-007: both `ArtifactContentRejectedError` raise sites reach the
    caller verbatim (type-prefixed), never as the generic `content_validation failed`.
    """
    stage = _FakeRestoreStage(error=ArtifactContentRejectedError(message))

    with pytest.raises(RestoreStageFailedError) as excinfo:
        run_restoration(
            _valid_request(), config=_config(), actor="x", dependencies=_build_dependencies(stage)
        )

    assert excinfo.value.stage == "content_validation"
    assert excinfo.value.reason == f"ArtifactContentRejectedError: {message}"


def test_run_restoration_cap_trims_the_reason_tail_never_the_rejected_label_prefix() -> None:
    """GH #104 / AC-BI-008: the rejected name sits at a fixed offset (<= 60 chars) of a
    message that is scrubbed then capped at 300 -- so the cap can only ever trim the
    echoed allow-list tail. Bound: a label longer than ~240 chars would itself be cut,
    and a path- or host:port-shaped label is rewritten by `_scrub_text` by design.
    """
    message = (
        f"node label 'PracticeArea' is not in the allow-list {_REPO_ROOT}/x at 10.0.0.5:6379 "
        + "Z" * 400
    )
    stage = _FakeRestoreStage(error=ArtifactContentRejectedError(message))

    with pytest.raises(RestoreStageFailedError) as excinfo:
        run_restoration(
            _valid_request(), config=_config(), actor="x", dependencies=_build_dependencies(stage)
        )

    reason = excinfo.value.reason
    assert excinfo.value.stage == "content_validation"
    assert reason.startswith(
        "ArtifactContentRejectedError: node label 'PracticeArea' is not in the allow-list"
    )
    assert "10.0.0.5:6379" not in reason
    assert str(_REPO_ROOT) not in reason
    assert len(reason) == _STAGE_REASON_MAX_LEN


@pytest.mark.parametrize(
    ("delegate_error", "expected_stage"),
    [
        (RestoreConcurrencyConflictError("exhausted retries on policy_system"), "concurrency"),
        (RuntimeError("unexpected boom at 10.0.0.5:6379"), "restore"),
    ],
    ids=["concurrency_conflict", "runtime_error"],
)
def test_run_restoration_keeps_non_whitelisted_delegate_errors_generic(
    delegate_error: Exception, expected_stage: str, configured_logging: Path
) -> None:
    """GH #104 boundary: only `ArtifactContentRejectedError` joined the safe-verbatim
    list. `RestoreConcurrencyConflictError` (its message embeds the single-tenant graph
    name) and unexpected failures still collapse to the generic `<stage> failed`.
    """
    _ = configured_logging
    stage = _FakeRestoreStage(error=delegate_error)

    with pytest.raises(RestoreStageFailedError) as excinfo:
        run_restoration(
            _valid_request(), config=_config(), actor="x", dependencies=_build_dependencies(stage)
        )

    assert excinfo.value.stage == expected_stage
    assert excinfo.value.reason == f"{expected_stage} failed"


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


def test_run_restoration_forwards_the_owner_to_the_delegate() -> None:
    """Issue #183: the caller's verified `(sub, iss)` reaches `restore_instrument` as `owner`."""
    stage = _FakeRestoreStage()
    owner = ("alice@example.com", "https://idp.example/")

    run_restoration(
        _valid_request(),
        config=_config(),
        actor="x",
        dependencies=_build_dependencies(stage),
        owner=owner,
    )

    assert stage.calls[0].owner == owner


def test_run_restoration_passes_no_owner_when_none_is_given() -> None:
    stage = _FakeRestoreStage()

    run_restoration(
        _valid_request(), config=_config(), actor="x", dependencies=_build_dependencies(stage)
    )

    assert stage.calls[0].owner is None


def test_masked_restore_failure_is_logged_server_side_with_traceback(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """AC-BI-012: the exception class, message, traceback, instrument id and caller are logged."""
    emitter, log_path = make_emitter()
    stage = _FakeRestoreStage(error=RuntimeError("boom at 10.0.0.5:6379"))

    with pytest.raises(RestoreStageFailedError):
        run_restoration(
            _valid_request(),
            config=_config(),
            actor="caller-host",
            dependencies=_build_dependencies(stage),
            emitter=emitter,
        )
    emitter.flush()

    (entry,) = [e for e in read_lines(log_path) if e.get("outcome") == "failed"]
    assert entry["component"] == "restore"
    assert entry["entity_id"] == "CRA-1.0"
    assert entry["caller"] == "caller-host"
    assert entry["exception_type"] == "RuntimeError"
    assert entry["detail"] == "boom at 10.0.0.5:6379"
    assert "Traceback (most recent call last)" in cast("str", entry["traceback"])


def test_masked_restore_failure_keeps_the_client_message_scrubbed(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-013: logging the real cause server-side does not change what the client sees."""
    emitter, _log_path = make_emitter()
    stage = _FakeRestoreStage(error=RuntimeError("boom at 10.0.0.5:6379 /srv/secret/path"))

    with pytest.raises(RestoreStageFailedError) as excinfo:
        run_restoration(
            _valid_request(),
            config=_config(),
            actor="x",
            dependencies=_build_dependencies(stage),
            emitter=emitter,
        )

    assert excinfo.value.reason == "restore failed"
    assert "10.0.0.5" not in str(excinfo.value)
    assert "/srv/secret" not in str(excinfo.value)


def test_whitelisted_restore_failure_is_not_logged_as_a_masked_failure(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    """A safe-verbatim failure already reaches the client in full; nothing is logged as masked."""
    emitter, log_path = make_emitter()
    stage = _FakeRestoreStage(error=ArtifactContentRejectedError("label not allow-listed"))

    with pytest.raises(RestoreStageFailedError):
        run_restoration(
            _valid_request(),
            config=_config(),
            actor="x",
            dependencies=_build_dependencies(stage),
            emitter=emitter,
        )
    emitter.flush()

    assert read_lines(log_path) == []
