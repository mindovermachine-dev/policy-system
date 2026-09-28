"""HTTP tests for `POST /restorations` (D5, PLAN.md Slice 6.4).

Mirrors `test_ingestions_catalog.py`'s style: `TestClient` +
`app.dependency_overrides` supplying a fake `RestoreDependencies` bundle, so
request validation, the restore hand-off, and error mapping are exercised
without a real graph or LLM.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from api._fakes import install_compliance_officer_grant, install_no_principal
from ps_service.api.dependencies import provide_restore_dependencies
from ps_service.api.restore_orchestration import RestoreDependencies
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.main import create_app
from ps_service.restore.errors import ArtifactContentRejectedError, ArtifactIntegrityError
from ps_service.restore.models import RestoreOutcome

if TYPE_CHECKING:
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.restore.models import RestoreArtifact

_REASON_MAX_LEN = 300  # error_handlers._REASON_MAX_LEN, pinned by tests/api/test_error_handlers.py

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


def _valid_body() -> dict[str, object]:
    return {
        "instrument_id": "CRA-1.0",
        "manifest": _MANIFEST_PAYLOAD,
        "baseline_blob_base64": base64.b64encode(b'{"nodes": []}').decode("ascii"),
        "native_blob_base64": base64.b64encode(b'{"nodes": []}').decode("ascii"),
    }


@dataclass
class _FakeDb:
    """Never actually touched by these fakes."""


class _FakeRestoreStage:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.call_count = 0
        self._error = error

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
        _ = (artifact, db, single_tenant_graph_name, similarity_threshold, actor, emitter)
        self.call_count += 1
        if self._error is not None:
            raise self._error
        return RestoreOutcome(
            instrument_id="CRA-1.0",
            stages=("verified", "staged", "merged_and_finalized"),
        )


def _fake_dependencies(stage: _FakeRestoreStage) -> RestoreDependencies:
    return RestoreDependencies(
        open_db=lambda config: cast("FalkorDB", _FakeDb()),
        single_tenant_graph_name=lambda config: "policy_system",
        restore=stage,
    )


def _app_config() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        company_merge_similarity_threshold=0.83,
        is_local_test_bypass_active=True,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _client_with_fake(stage: _FakeRestoreStage) -> TestClient:
    app = create_app(_app_config())
    app.dependency_overrides[provide_restore_dependencies] = lambda: _fake_dependencies(stage)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _grant_compliance_officer(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test below drives a caller holding `ComplianceOfficer` by default (issue #145).

    `POST /restorations` is now gated behind `require_access_role`, which has
    no local-test-bypass carve-out (`AccessDeniedError` whenever
    `get_principal` returns `None` -- true for every test's own
    `is_local_test_bypass_active=True` config absent this fixture, PLAN.md
    §3.2). The dedicated denial-proof tests below re-monkeypatch this away
    for their own scenario (the same `monkeypatch` fixture instance, so the
    later call simply wins).
    """
    install_compliance_officer_grant(monkeypatch, granted=True)


def test_valid_restoration_body_returns_200_with_expected_shape() -> None:
    stage = _FakeRestoreStage()
    client = _client_with_fake(stage)

    response = client.post("/restorations", json=_valid_body())

    assert response.status_code == 200
    body = response.json()
    assert body["instrument_id"] == "CRA-1.0"
    assert [s["stage"] for s in body["stages"]] == [
        "verified",
        "staged",
        "merged_and_finalized",
    ]
    assert stage.call_count == 1


def test_integrity_failure_returns_422() -> None:
    stage = _FakeRestoreStage(error=ArtifactIntegrityError("checksum mismatch"))
    client = _client_with_fake(stage)

    response = client.post("/restorations", json=_valid_body())

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "restore_artifact_rejected"


def test_stage_failure_returns_502_naming_the_stage() -> None:
    stage = _FakeRestoreStage(error=RuntimeError("unexpected boom"))
    client = _client_with_fake(stage)

    response = client.post("/restorations", json=_valid_body())

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "restore_stage_failed"
    assert body["error"]["failing_stage"]


def test_content_rejection_returns_502_whose_message_names_the_rejected_label() -> None:
    """GH #104 / AC-BI-007 end-to-end: FastAPI app -> route -> orchestration -> 502 handler.

    The body's `error.message` (the AC's "reason") carries the validator's own text
    naming the rejected label, never the generic `content_validation failed`.
    """
    stage = _FakeRestoreStage(
        error=ArtifactContentRejectedError(
            "node label 'PracticeArea' is not in the allow-list ['Capability', 'Control']"
        )
    )
    client = _client_with_fake(stage)

    response = client.post("/restorations", json=_valid_body())

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "restore_stage_failed"
    assert body["error"]["failing_stage"] == "content_validation"
    message = body["error"]["message"]
    assert "node label 'PracticeArea'" in message
    assert message != "content_validation failed"
    assert len(message) <= _REASON_MAX_LEN


def test_malformed_body_returns_422_and_never_calls_the_delegate() -> None:
    stage = _FakeRestoreStage()
    client = _client_with_fake(stage)

    response = client.post("/restorations", json={})

    assert response.status_code == 422
    assert stage.call_count == 0


@pytest.mark.parametrize("path", ["/restorations"])
def test_restorations_route_with_compliance_officer_grant_is_never_401_or_403(path: str) -> None:
    """Issue #145: a caller holding `ComplianceOfficer` is never turned away by the gate.

    Renamed from the pre-#145 `..._is_unauthenticated_never_401_or_403` -- the
    route is no longer unauthenticated-by-design (it now requires
    `ComplianceOfficer`); this proves the gate itself introduces no *spurious*
    401/403 for a caller who does hold the role, relying on the file's own
    autouse `_grant_compliance_officer` fixture exactly like every other
    happy-path test here.
    """
    stage = _FakeRestoreStage()
    client = _client_with_fake(stage)

    response = client.post(path, json=_valid_body())

    assert response.status_code not in (401, 403)


def test_no_principal_at_all_is_denied_with_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #145, AC-BI-003/006: no verified principal (the file's own bypass default,
    undoing the autouse grant) -- 403, matching the existing `AccessDeniedError` contract,
    and the restore delegate is never called.
    """
    install_no_principal(monkeypatch)
    stage = _FakeRestoreStage()
    client = _client_with_fake(stage)

    response = client.post("/restorations", json=_valid_body())

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "access_denied"
    assert body["error"]["message"] == "You do not have the required access role for this action."
    assert stage.call_count == 0


def test_authenticated_user_without_compliance_officer_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified caller holding no elevated role at all -- still 403, delegate never called."""
    install_compliance_officer_grant(monkeypatch, granted=False)
    stage = _FakeRestoreStage()
    client = _client_with_fake(stage)

    response = client.post("/restorations", json=_valid_body())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert stage.call_count == 0


def test_system_admin_without_explicit_grant_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: `SystemAdmin` alone does not implicitly satisfy `ComplianceOfficer`."""
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_ADMIN})
    )
    stage = _FakeRestoreStage()
    client = _client_with_fake(stage)

    response = client.post("/restorations", json=_valid_body())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert stage.call_count == 0


def test_system_owner_without_explicit_grant_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: `SystemOwner` alone does not implicitly satisfy `ComplianceOfficer` either --
    no hierarchy override, mirroring `PolicyManager`'s own existing non-hierarchical contract.
    """
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_OWNER})
    )
    stage = _FakeRestoreStage()
    client = _client_with_fake(stage)

    response = client.post("/restorations", json=_valid_body())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert stage.call_count == 0
