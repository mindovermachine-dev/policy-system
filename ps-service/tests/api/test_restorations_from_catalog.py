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

import dataclasses
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, cast

import pytest
from fastapi.testclient import TestClient

from api._audit_fakes import InMemoryAuditStore
from api._fakes import (
    FakeCuratedArtifactTransport,
    FakeFailingCuratedSourceTransport,
    compliance_officer_principal,
    install_compliance_officer_grant,
    install_no_principal,
)
from ps_service.api.dependencies import (
    get_principal,
    provide_audit_store,
    provide_restore_from_catalog_dependencies,
)
from ps_service.api.restore_orchestration import CatalogRestoreDependencies
from ps_service.audit import AuditPostgresUnavailableError
from ps_service.auth.models import Principal
from ps_service.authz.models import AccessRole
from ps_service.config import ServiceConfig
from ps_service.curated_source.artifact_client import fetch_artifact
from ps_service.curated_source.catalog_client import fetch_catalog
from ps_service.curated_source.resolve import EffectiveCatalogSource
from ps_service.main import create_app
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreOutcome
from ps_service.runtime_config import RuntimeConfigUnavailableError

if TYPE_CHECKING:
    import urllib.request
    from pathlib import Path

    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]
    from fastapi import FastAPI

    from ps_service.api.catalog import CuratedInstrumentEntry
    from ps_service.curated_source.artifact_client import FetchArtifactCall, FetchedArtifact
    from ps_service.curated_source.catalog_client import FetchCatalogCall
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


_VALID_CATALOG_ENTRY: dict[str, object] = {
    "instrument_id": _INSTRUMENT_ID,
    "celex": "32024R2847",
    "title": "Cyber Resilience Act",
    "source_type": "external",
    "jurisdiction": "EU",
    "short_name": "CRA",
    "version": "1.0",
}


def _valid_transport() -> FakeCuratedArtifactTransport:
    return FakeCuratedArtifactTransport(
        {
            "catalog.json": json.dumps([_VALID_CATALOG_ENTRY]).encode("utf-8"),
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
        owner: tuple[str, str] | None = None,
    ) -> RestoreOutcome:
        _ = (db, emitter)
        self.calls.append(
            {
                "instrument_id": artifact.manifest.instrument_id,
                "single_tenant_graph_name": single_tenant_graph_name,
                "similarity_threshold": similarity_threshold,
                "actor": actor,
                "source": source,
                "owner": owner,
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


def _fetch_catalog_through(transport: CuratedSourceTransport) -> FetchCatalogCall:
    def _call(base_url: str) -> tuple[CuratedInstrumentEntry, ...]:
        return fetch_catalog(base_url, transport=transport)

    return _call


def _fake_dependencies(
    transport: CuratedSourceTransport, stage: _FakeCatalogRestoreStage
) -> CatalogRestoreDependencies:
    def _resolve_effective_source(config: ServiceConfig) -> EffectiveCatalogSource:
        return EffectiveCatalogSource(url=config.curated_source_base_url, is_override=False)

    return CatalogRestoreDependencies(
        fetch_artifact=_fetch_artifact_through(transport),
        fetch_catalog=_fetch_catalog_through(transport),
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
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _fresh_audit_store() -> InMemoryAuditStore:
    return InMemoryAuditStore()


def _client_with_fake(
    transport: CuratedSourceTransport,
    stage: _FakeCatalogRestoreStage,
    *,
    audit_store: InMemoryAuditStore | None = None,
) -> TestClient:
    app = create_app(_app_config())
    store = audit_store if audit_store is not None else InMemoryAuditStore()
    app.dependency_overrides[provide_audit_store] = lambda: store
    app.dependency_overrides[provide_restore_from_catalog_dependencies] = lambda: (
        _fake_dependencies(transport, stage)
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _grant_compliance_officer(  # pyright: ignore[reportUnusedFunction]  # pytest autouse fixture — invoked by name-collection, never referenced in-module
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test below drives a caller holding `ComplianceOfficer` by default (issue #145).

    `POST /restorations/from-catalog` (and, in one cross-route smoke test
    below, `POST /restorations` too) is now gated behind `require_access_role`,
    which has no local-test-bypass carve-out (PLAN.md §3.2). The dedicated
    denial-proof tests below re-monkeypatch this away for their own scenario
    (the same `monkeypatch` fixture instance, so the later call simply wins).
    """
    install_compliance_officer_grant(monkeypatch, granted=True)


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


def test_route_with_compliance_officer_grant_is_never_401_or_403() -> None:
    """Issue #145: renamed from the pre-#145 `..._is_unauthenticated_never_401_or_403` --
    the route now requires `ComplianceOfficer`; this proves a caller who holds it (the
    file's own autouse `_grant_compliance_officer` fixture) is never turned away.
    """
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code not in (401, 403)


def test_no_principal_at_all_is_denied_with_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #145, AC-BI-003/006: no verified principal -- 403, delegate never called."""
    install_no_principal(monkeypatch)
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "access_denied"
    assert body["error"]["message"] == "You do not have the required access role for this action."
    assert stage.calls == []


def test_authenticated_user_without_compliance_officer_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified caller holding no elevated role at all -- still 403, delegate never called."""
    install_compliance_officer_grant(monkeypatch, granted=False)
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert stage.calls == []


def test_system_admin_without_explicit_grant_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: `SystemAdmin` alone does not implicitly satisfy `ComplianceOfficer`."""
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_ADMIN})
    )
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert stage.calls == []


def test_system_owner_without_explicit_grant_is_denied_with_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BI-005: `SystemOwner` alone does not implicitly satisfy `ComplianceOfficer` either."""
    install_compliance_officer_grant(
        monkeypatch, granted=False, roles=frozenset({AccessRole.SYSTEM_OWNER})
    )
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"
    assert stage.calls == []


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


def test_restore_from_catalog_fails_closed_when_override_read_fails() -> None:
    """AC-BI-010: an override-read failure is a named 503 and nothing is fetched or restored."""
    transport = _valid_transport()
    stage = _FakeCatalogRestoreStage()

    def _failing_resolve(config: ServiceConfig) -> EffectiveCatalogSource:
        del config
        raise RuntimeConfigUnavailableError(
            "The runtime configuration store is temporarily unavailable."
        )

    dependencies = dataclasses.replace(
        _fake_dependencies(transport, stage), resolve_effective_source=_failing_resolve
    )
    app = create_app(_app_config())
    app.dependency_overrides[provide_audit_store] = _fresh_audit_store
    app.dependency_overrides[provide_restore_from_catalog_dependencies] = lambda: dependencies

    response = TestClient(app, raise_server_exceptions=False).post(
        "/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID}
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "catalog_source_override_unavailable"
    assert stage.calls == []
    assert transport.requests == []


def test_verified_caller_is_passed_to_the_delegate_as_the_owner_of_restored_drafts() -> None:
    """Issue #183, AC-BI-001: the route hands the verified `(sub, iss)` to the restore as owner."""
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)
    cast("FastAPI", client.app).dependency_overrides[get_principal] = lambda: Principal(
        sub="alice@example.com", iss="https://idp.example/"
    )

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 200
    assert stage.calls[0]["owner"] == ("alice@example.com", "https://idp.example/")


def test_no_verified_principal_means_no_owner_is_passed_to_the_delegate() -> None:
    """With no verified principal the owner is `None`; the restore refuses Policy content then."""
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 200
    assert stage.calls[0]["owner"] is None


# --- issue #184: case-insensitive instrument_id resolution against the catalog -------


def test_lowercase_instrument_id_resolves_and_fetches_the_canonical_id() -> None:
    """AC-BI-001: `cra-1.0` resolves against the catalog's `CRA-1.0` and fetches under it."""
    stage = _FakeCatalogRestoreStage()
    transport = _valid_transport()
    client = _client_with_fake(transport, stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": "cra-1.0"})

    assert response.status_code == 200
    assert response.json()["instrument_id"] == _INSTRUMENT_ID
    requested_urls = [req.full_url for req in transport.requests]
    assert any(f"/{_INSTRUMENT_ID}/manifest.json" in url for url in requested_urls)
    assert not any("/cra-1.0/" in url for url in requested_urls)


def test_catalog_fetch_failure_surfaces_as_the_existing_curated_source_unavailable_error() -> None:
    """AC-BI-005: a `catalog.json` fetch failure is the same 502, not a masked case error."""
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(FakeFailingCuratedSourceTransport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "curated_source_unavailable"
    assert stage.calls == []


def test_audit_delegate_receives_the_canonical_id_not_the_callers_spelling() -> None:
    """AC-BI-006: the restore delegate's manifest carries the canonical id, not `cra-1.0`."""
    stage = _FakeCatalogRestoreStage()
    client = _client_with_fake(_valid_transport(), stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": "cra-1.0"})

    assert response.status_code == 200
    assert stage.calls[0]["instrument_id"] == _INSTRUMENT_ID


_AMBIGUOUS_CATALOG_ENTRIES: list[dict[str, object]] = [
    _VALID_CATALOG_ENTRY,
    {**_VALID_CATALOG_ENTRY, "instrument_id": _INSTRUMENT_ID.lower()},
]


def _ambiguous_transport() -> FakeCuratedArtifactTransport:
    return FakeCuratedArtifactTransport(
        {
            "catalog.json": json.dumps(_AMBIGUOUS_CATALOG_ENTRIES).encode("utf-8"),
            "manifest.json": json.dumps(_VALID_MANIFEST).encode("utf-8"),
            "baseline.json": _BASELINE_BYTES,
            "native.json": _NATIVE_BYTES,
        }
    )


def test_ambiguous_case_collision_is_rejected_with_409_and_never_fetches_the_artifact() -> None:
    """AC-BI-003: two catalog entries differing only by case -> 409, nothing fetched.

    Proven against both spellings a caller might use (`CRA-1.0` and `cra-1.0`): the
    collision is present in the catalog either way, so neither spelling should ever
    resolve to a single canonical id.
    """
    for requested_id in (_INSTRUMENT_ID, _INSTRUMENT_ID.lower()):
        stage = _FakeCatalogRestoreStage()
        transport = _ambiguous_transport()
        client = _client_with_fake(transport, stage)

        response = client.post("/restorations/from-catalog", json={"instrument_id": requested_id})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "restore_instrument_id_ambiguous"
        assert stage.calls == []
        requested_filenames = {req.full_url.rsplit("/", 1)[-1] for req in transport.requests}
        assert requested_filenames == {"catalog.json"}


_NO_MATCH_CATALOG_ENTRIES: list[dict[str, object]] = [
    _VALID_CATALOG_ENTRY,
    {
        **_VALID_CATALOG_ENTRY,
        "instrument_id": "GDPR-1.0",
        "celex": "32016R0679",
        "title": "General Data Protection Regulation",
        "short_name": "GDPR",
    },
]


def _no_match_transport() -> FakeCuratedArtifactTransport:
    return FakeCuratedArtifactTransport(
        {
            "catalog.json": json.dumps(_NO_MATCH_CATALOG_ENTRIES).encode("utf-8"),
            "manifest.json": json.dumps(_VALID_MANIFEST).encode("utf-8"),
            "baseline.json": _BASELINE_BYTES,
            "native.json": _NATIVE_BYTES,
        }
    )


def test_no_catalog_match_in_any_case_returns_404_naming_closest_ids_and_never_fetches() -> None:
    """AC-BI-004: no catalog entry matches any case -> 404, naming the closest id, no fetch."""
    stage = _FakeCatalogRestoreStage()
    transport = _no_match_transport()
    client = _client_with_fake(transport, stage)

    response = client.post("/restorations/from-catalog", json={"instrument_id": "cra-9.9"})

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "restore_instrument_id_not_found"
    assert _INSTRUMENT_ID in body["error"]["message"]
    assert stage.calls == []
    requested_filenames = {req.full_url.rsplit("/", 1)[-1] for req in transport.requests}
    assert requested_filenames == {"catalog.json"}


# --- issue #195: instrument.restore audit rows ---------------------------------------


def test_post_from_catalog_audits_the_principal() -> None:
    """AC-BI-001/002: the opening and terminal rows carry the verified caller as actor.

    The ComplianceOfficer gate (autouse) presents `compliance_officer_principal()`; the audit
    context is built from the same `get_principal`, so that principal is the row actor. (Under the
    local-test bypass this gated route is unreachable; the sentinel path is covered by the
    `provide_audit_context` and MCP tests.)
    """
    store = InMemoryAuditStore()
    client = _client_with_fake(_valid_transport(), _FakeCatalogRestoreStage(), audit_store=store)
    principal = compliance_officer_principal()

    response = client.post("/restorations/from-catalog", json={"instrument_id": "cra-1.0"})

    assert response.status_code == 200
    assert [(r.actor_subject, r.actor_issuer) for r in store.rows] == [
        (principal.sub, principal.iss)
    ] * 2
    assert [(r.action, r.resource_id, r.details["status"]) for r in store.rows] == [
        ("instrument.restore", _INSTRUMENT_ID, "started"),
        ("instrument.restore", _INSTRUMENT_ID, "succeeded"),
    ]


def test_post_from_catalog_returns_503_when_audit_unavailable() -> None:
    """AC-BI-011: the opening row cannot be written -> 503, nothing restored."""
    stage = _FakeCatalogRestoreStage()
    store = InMemoryAuditStore(fail_on_outcome={"applied": AuditPostgresUnavailableError("db")})
    client = _client_with_fake(_valid_transport(), stage, audit_store=store)

    response = client.post("/restorations/from-catalog", json={"instrument_id": _INSTRUMENT_ID})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "audit_trail_unavailable"
    assert stage.calls == []
