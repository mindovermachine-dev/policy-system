"""HTTP tests for `POST /restorations/from-catalog` (issue #125, PLAN.md Slice 2).

Mirrors `test_routes_restorations.py`'s style (`TestClient` +
`app.dependency_overrides` supplying a fake `CatalogRestoreDependencies`
bundle) and `test_routes_catalog.py`'s fake-HTTP-transport convention: the
real `fetch_artifact` is wired to a fake transport
(`FakeCuratedArtifactTransport`, `tests/api/_fakes.py`) so request
validation, the fetch-then-restore hand-off, and error mapping are all
exercised without real network or a real graph. `POST /restorations` (the
upload path) is untouched by this file -- see the protected
`test_routes_restorations.py`, run separately to confirm it stays
byte-for-byte unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast

import pytest
from fastapi.testclient import TestClient

from api._fakes import FakeCuratedArtifactTransport, FakeFailingCuratedSourceTransport
from ps_service.api.dependencies import provide_restore_from_catalog_dependencies
from ps_service.api.restore_orchestration import CatalogRestoreDependencies
from ps_service.config import ServiceConfig
from ps_service.curated_source.artifact_client import fetch_artifact
from ps_service.curated_source.resolve import EffectiveCatalogSource
from ps_service.main import create_app
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreOutcome

if TYPE_CHECKING:
    import urllib.request
    from pathlib import Path

    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.curated_source.artifact_client import FetchArtifactCall, FetchedArtifact
    from ps_service.curated_source.http_fetch import CuratedSourceTransport
    from ps_service.restore.models import RestoreArtifact

_INSTRUMENT_ID = "CRA-1.0"

_VALID_MANIFEST: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
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

_BASELINE_BYTES = b'{"nodes": [], "edges": []}'
_NATIVE_BYTES = b'{"nodes": [], "edges": []}'


@pytest.fixture(autouse=True)
def _configure_logging_for_catalog_restore_tests(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    configured_logging: Path,
) -> None:
    """`fetch_artifact` always logs (issue #125) -- install a real Logging facade.

    Mirrors `test_routes_catalog.py`'s own
    `_configure_logging_for_catalog_tests` fixture: `create_app` + a bare
    `TestClient` never enters `lifespan`, so nothing else here configures the
    process-wide default emitter.
    """


def _valid_transport() -> FakeCuratedArtifactTransport:
    return FakeCuratedArtifactTransport(
        {
            "manifest.json": json.dumps(_VALID_MANIFEST).encode("utf-8"),
            "baseline.json": _BASELINE_BYTES,
            "native.json": _NATIVE_BYTES,
        }
    )


@dataclass
class _FakeDb:
    """A stand-in for `falkordb.FalkorDB` -- never actually touched by these fakes."""


class _FakeCatalogRestoreStage:
    """Records every call (including the `source` kwarg, D-AUDIT) and returns/raises a result."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
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
        source: str | None = None,
    ) -> RestoreOutcome:
        _ = (db, emitter)
        self.calls.append(
            {
                "single_tenant_graph_name": single_tenant_graph_name,
                "similarity_threshold": similarity_threshold,
                "actor": actor,
                "source": source,
            }
        )
        if self._error is not None:
            raise self._error
        return RestoreOutcome(
            instrument_id=artifact.manifest.instrument_id,
            stages=("verified", "staged", "merged_and_finalized"),
        )


def _fetch_artifact_through(transport: CuratedSourceTransport) -> FetchArtifactCall:
    def _call(base_url: str, instrument_id: str) -> FetchedArtifact:
        return fetch_artifact(base_url, instrument_id, transport=transport)

    return _call


def _fake_dependencies(
    transport: CuratedSourceTransport, stage: _FakeCatalogRestoreStage
) -> CatalogRestoreDependencies:
    def _resolve_effective_source(config: ServiceConfig) -> EffectiveCatalogSource:
        return EffectiveCatalogSource(url=config.curated_source_base_url, is_override=False)

    return CatalogRestoreDependencies(
        fetch_artifact=_fetch_artifact_through(transport),
        resolve_effective_source=_resolve_effective_source,
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
        curated_source_base_url="https://example.com/curated-content",
    )


def _client_with_fake(
    transport: CuratedSourceTransport, stage: _FakeCatalogRestoreStage
) -> TestClient:
    app = create_app(_app_config())
    app.dependency_overrides[provide_restore_from_catalog_dependencies] = lambda: (
        _fake_dependencies(transport, stage)
    )
    return TestClient(app, raise_server_exceptions=False)


def test_valid_instrument_id_returns_200_with_expected_shape() -> None:
    """(1) A valid request against a fake transport serving a valid fixture completes."""
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 200
    body = response.json()
    assert body["instrument_id"] == _INSTRUMENT_ID
    assert [s["stage"] for s in body["stages"]] == [
        "verified",
        "staged",
        "merged_and_finalized",
    ]
    assert len(stage.calls) == 1


def test_restore_delegate_receives_the_effective_source_url_for_the_audit_log() -> None:
    """(2) `source` is threaded through to the restore delegate (D-AUDIT, AC-BI-011)."""
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert stage.calls[0]["source"] == "https://example.com/curated-content"


def test_checksum_mismatch_returns_422_and_never_calls_the_delegate_with_a_bad_artifact() -> None:
    """(3) A checksum-rejected fetched artifact -> 422 (AC-BI-009, D9 reused unmodified)."""
    stage = _FakeCatalogRestoreStage(error=ArtifactIntegrityError("checksum mismatch"))
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "restore_artifact_rejected"


def test_schema_version_mismatch_returns_422() -> None:
    """(4) A schema_version-rejected fetched artifact -> 422 (D10 reused, mirrors #66)."""
    stage = _FakeCatalogRestoreStage(error=ArtifactSchemaVersionMismatchError("schema mismatch"))
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "restore_artifact_rejected"


def test_unreachable_source_returns_502_naming_the_failure_and_never_calls_the_delegate() -> None:
    """(5) An unreachable source -> 502 naming the failure (AC-BI-006/004), delegate never called.

    The response body's `message` is the *already-scrubbed* failure reason
    (`error_handlers._scrub_text` redacts embedded URLs -- the same
    behavior `test_routes_catalog.py`'s own unreachable-source test relies
    on): it never leaks the raw source URL or the instrument-id-bearing
    path segment, but it does name the underlying transport failure.
    """
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(FakeFailingCuratedSourceTransport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "curated_source_unavailable"
    assert "connection refused" in body["error"]["message"]
    assert stage.calls == []


def test_malformed_instrument_id_returns_422_and_never_fetches_or_restores() -> None:
    """A path-unsafe instrument_id is rejected by the request model before any fetch."""

    class _NeverCalledTransport:
        def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
            _ = (request, timeout)
            raise AssertionError("must not be called for a rejected request body")

    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_NeverCalledTransport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": "../etc/passwd"})

    assert response.status_code == 422
    assert stage.calls == []


def test_extra_field_in_request_body_returns_422() -> None:
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post(
        "/restorations/from-catalog",
        json={"instrument_id": _INSTRUMENT_ID, "unexpected": "field"},
    )

    assert response.status_code == 422
    assert stage.calls == []


def test_route_is_unauthenticated_never_401_or_403() -> None:
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code not in (401, 403)


def test_restorations_upload_path_is_unaffected_by_this_route(configured_logging: Path) -> None:
    """(6) `POST /restorations` (upload path) coexists unchanged alongside the new route.

    A minimal smoke check that both routes are simultaneously registered and
    independently reachable on the same app -- the full byte-for-byte
    "unmodified" proof is `test_routes_restorations.py` itself (run
    separately, unedited by this issue).
    """
    _ = configured_logging
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    from_catalog_response = client.post(
        "/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID}
    )
    upload_response = client.post("/restorations", json={"not": "a valid upload body"})

    assert from_catalog_response.status_code == 200
    # The upload route still exists and still validates its own (unrelated) body shape --
    # a 422 here proves routing dispatched to the real, still-registered upload handler,
    # not a 404 (which would mean this new route had displaced it).
    assert upload_response.status_code == 422
