"""``run_restoration`` with the REAL ``restore_instrument`` delegate, DB-free (GH #104, AC-BI-007).

Closes the gap between the synthetic-message tests (``test_restore_orchestration.py``,
``test_routes_restorations.py``) and the real validator: the rejected label is put into
``str(exc)`` by ``schema_allowlist.validate_serialized_graph`` itself and classified by the real
``_classify_restore_failure``. No FalkorDB -- content validation precedes every ``db`` call
(``tests/restore/test_restore_instrument_content_validation.py``), so ``open_db`` returns a
never-touched sentinel. The delegate emits its audit entries through the process default emitter
(the wrapper passes no ``emitter``), hence the explicit ``facade.configure``; the root autouse
``_isolate_logging`` fixture's ``reset_for_tests()`` tears it down (and re-arms the atexit
guard) afterwards. ``tests/api/conftest.py::configured_logging`` is deliberately not used: under
``--import-mode=importlib`` an ``api/`` file argument listed after ``tests/test_main.py`` does
not see ``api/conftest.py`` fixtures, which is exactly the shape of this slice's exit command.

The checksum-correct artifact is built here with the same real codec calls
``tests/restore/_fixtures.py::build_restore_artifact`` uses (``to_json_bytes`` +
``checksum_bytes``) rather than imported from it: under ``--import-mode=importlib`` a sibling
test package (``restore``) is only importable once one of its own modules has been collected,
so a cross-directory runtime import would pass in the full suite and fail standalone. Per-file
duplication is this suite's documented convention (``tests/restore/conftest.py``).
"""

from __future__ import annotations

import base64
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

import pytest

from ps_service.api.errors import RestoreStageFailedError
from ps_service.api.models import RestorationRequest
from ps_service.api.restore_orchestration import RestoreDependencies, run_restoration
from ps_service.config import ServiceConfig
from ps_service.domain_mapper import DOMAIN_SCHEMA_VERSION
from ps_service.export.models import InstrumentManifest, SerializedGraph, SerializedNode
from ps_service.export.serialize import checksum_bytes, to_json_bytes
from ps_service.logging import facade
from ps_service.restore.models import RestoreArtifact
from ps_service.restore.restore_instrument import restore_instrument

if TYPE_CHECKING:
    from pathlib import Path

    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

_INSTRUMENT_ID = "GH104-1.0"
_NEVER_TOUCHED_DB = cast("FalkorDB", object())
_REASON_MAX_LEN = 300  # == restore_orchestration._STAGE_REASON_MAX_LEN (pinned by its own tests)


def _checksum_correct_artifact(
    *, native_graph: SerializedGraph, baseline_graph: SerializedGraph
) -> RestoreArtifact:
    """Real ``to_json_bytes`` + ``checksum_bytes``, so the 422 integrity path is never taken."""
    native_blob = to_json_bytes(native_graph)
    baseline_blob = to_json_bytes(baseline_graph)
    manifest = InstrumentManifest(
        instrument_id=_INSTRUMENT_ID,
        celex=None,
        title=_INSTRUMENT_ID,
        short_name="GH104",
        version="1.0",
        source_type="external",
        jurisdiction=None,
        schema_version=DOMAIN_SCHEMA_VERSION,
        exported_at="2026-09-04T00:00:00Z",
        baseline_sha256=checksum_bytes(baseline_blob),
        native_sha256=checksum_bytes(native_blob),
    )
    return RestoreArtifact(manifest=manifest, baseline_blob=baseline_blob, native_blob=native_blob)


def _artifact_with_a_baseline_only_violation() -> RestoreArtifact:
    return _checksum_correct_artifact(
        native_graph=SerializedGraph(
            nodes=(
                SerializedNode(label="RegulatoryInstrument", properties={"id": _INSTRUMENT_ID}),
            ),
            edges=(),
        ),
        baseline_graph=SerializedGraph(
            nodes=(SerializedNode(label="EvilLabel", properties={"id": "evil-1"}),), edges=()
        ),
    )


def _request_for(artifact: RestoreArtifact) -> RestorationRequest:
    return RestorationRequest.model_validate(
        {
            "instrument_id": artifact.manifest.instrument_id,
            "manifest": asdict(artifact.manifest),
            "baseline_blob_base64": base64.b64encode(artifact.baseline_blob).decode("ascii"),
            "native_blob_base64": base64.b64encode(artifact.native_blob).decode("ascii"),
        }
    )


def _config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        company_merge_similarity_threshold=0.9,
    )


def _real_delegate_dependencies() -> RestoreDependencies:
    return RestoreDependencies(
        open_db=lambda config: _NEVER_TOUCHED_DB,
        single_tenant_graph_name=lambda config: "__gh104_never_touched_single_tenant__",
        restore=restore_instrument,
    )


def test_real_validator_rejection_reason_names_the_label_through_the_real_classifier(
    tmp_path: Path,
) -> None:
    facade.configure(log_path=tmp_path / "restore.jsonl")

    with pytest.raises(RestoreStageFailedError) as excinfo:
        run_restoration(
            _request_for(_artifact_with_a_baseline_only_violation()),
            config=_config(),
            actor="127.0.0.1",
            dependencies=_real_delegate_dependencies(),
        )

    assert excinfo.value.stage == "content_validation"
    assert excinfo.value.reason.startswith(
        "ArtifactContentRejectedError: node label 'EvilLabel' is not in the allow-list"
    )
    assert len(excinfo.value.reason) <= _REASON_MAX_LEN
