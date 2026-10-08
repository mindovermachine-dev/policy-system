"""Audit emission of the restore orchestration (issue #195, AC-BI-002/003/008/010/011/015).

`run_restoration_from_catalog_source` (catalog) and `run_restoration` (upload) write an
`instrument.restore` opening row (`applied`, `status=started`) immediately before the restore
starts, then a terminal row. Hand-written structural fakes (no `unittest.mock`): a recording
`InMemoryAuditStore` whose ordered `events` list is shared with the fake restore delegate so
audit-before-effect is asserted on real ordering.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, cast

import pytest

from api._audit_fakes import InMemoryAuditStore
from ps_service.api.catalog import CuratedInstrumentEntry
from ps_service.api.errors import (
    CuratedSourceUnavailableError,
    RestoreArtifactRejectedError,
    RestoreInstrumentIdAmbiguousError,
    RestoreInstrumentIdNotFoundError,
    RestoreStageFailedError,
)
from ps_service.api.models import (
    CatalogRestorationRequest,
    RestorationManifestPayload,
    RestorationRequest,
)
from ps_service.api.restore_orchestration import (
    CatalogRestoreDependencies,
    RestoreDependencies,
    run_restoration,
    run_restoration_from_catalog_source,
)
from ps_service.audit import (
    AuditContext,
    AuditPersistenceError,
    AuditPostgresUnavailableError,
    AuditTrailUnavailableError,
)
from ps_service.config import ServiceConfig
from ps_service.curated_source.artifact_client import FetchedArtifact
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.resolve import EffectiveCatalogSource
from ps_service.export.models import InstrumentManifest
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreOutcome

if TYPE_CHECKING:
    from company_merge._fakes import MakeEmitter, ReadLines
    from falkordb import FalkorDB  # pyright: ignore[reportMissingTypeStubs]

    from ps_service.logging import LogEmitter
    from ps_service.restore.models import RestoreArtifact

_ACTOR = ("actor-sub", "https://issuer.example.com/")
_SOURCE_URL = "https://curated.internal.example.com/secret-path"
_CANONICAL_ID = "CRA-1.0"
_MANIFEST = InstrumentManifest(
    instrument_id=_CANONICAL_ID,
    celex="32024R2847",
    title="Cyber Resilience Act",
    short_name="CRA",
    version="1.0",
    source_type="external",
    jurisdiction="EU",
    schema_version="1",
    exported_at="2026-01-01T00:00:00Z",
    baseline_sha256="a" * 64,
    native_sha256="b" * 64,
)
_CATALOG_ENTRY = CuratedInstrumentEntry(
    instrument_id=_CANONICAL_ID,
    celex="32024R2847",
    title="Cyber Resilience Act",
    source_type="external",
    jurisdiction="EU",
    short_name="CRA",
    version="1.0",
)


class _Db:
    """Stand-in for `falkordb.FalkorDB`; never touched by the fake delegate."""


def _config(*, threshold: float | None = 0.83) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        company_merge_similarity_threshold=threshold,
        is_local_test_bypass_active=True,
        curated_source_base_url=_SOURCE_URL,
    )


class _Delegate:
    """A restore delegate that records itself on the shared ordered event list."""

    def __init__(self, store: InMemoryAuditStore, *, error: Exception | None = None) -> None:
        self._store = store
        self._error = error
        self.calls = 0

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
        del db, single_tenant_graph_name, similarity_threshold, actor, emitter, source, owner
        self.calls += 1
        self._store.events.append("restore")
        if self._error is not None:
            raise self._error
        return RestoreOutcome(
            instrument_id=artifact.manifest.instrument_id,
            stages=("verified", "staged", "merged_and_finalized"),
        )


def _catalog_deps(
    delegate: _Delegate,
    *,
    catalog_error: Exception | None = None,
    entries: tuple[CuratedInstrumentEntry, ...] = (_CATALOG_ENTRY,),
) -> CatalogRestoreDependencies:
    def _fetch_catalog(base_url: str) -> tuple[CuratedInstrumentEntry, ...]:
        del base_url
        if catalog_error is not None:
            raise catalog_error
        return entries

    def _fetch_artifact(base_url: str, instrument_id: str) -> FetchedArtifact:
        del base_url, instrument_id
        return FetchedArtifact(manifest=_MANIFEST, baseline_blob=b"{}", native_blob=b"{}")

    return CatalogRestoreDependencies(
        fetch_artifact=_fetch_artifact,
        fetch_catalog=_fetch_catalog,
        resolve_effective_source=lambda _config: EffectiveCatalogSource(
            url=_SOURCE_URL, is_override=False
        ),
        open_db=lambda _config: cast("FalkorDB", _Db()),
        single_tenant_graph_name=lambda _config: "policy_system",
        restore=delegate,
    )


def _run_catalog(
    store: InMemoryAuditStore,
    delegate: _Delegate,
    *,
    requested_id: str = "cra-1.0",
    config: ServiceConfig | None = None,
    deps: CatalogRestoreDependencies | None = None,
    emitter: LogEmitter | None = None,
) -> object:
    return run_restoration_from_catalog_source(
        CatalogRestorationRequest(instrument_id=requested_id),
        config=config or _config(),
        actor="127.0.0.1",
        dependencies=deps or _catalog_deps(delegate),
        owner=_ACTOR,
        audit=AuditContext(_ACTOR, store),
        emitter=emitter,
    )


def _statuses(store: InMemoryAuditStore) -> list[tuple[str, object, object]]:
    return [(r.outcome, r.details["status"], r.details.get("reason_code")) for r in store.rows]


def test_from_catalog_restore_writes_opening_then_terminal_succeeded_with_canonical_instrument_id() -> (  # noqa: E501
    None
):
    store = InMemoryAuditStore()

    _run_catalog(store, _Delegate(store), requested_id="cra-1.0")

    assert _statuses(store) == [("applied", "started", None), ("applied", "succeeded", None)]
    for row in store.rows:
        assert (row.actor_subject, row.actor_issuer) == _ACTOR
        assert (row.action, row.resource_type) == ("instrument.restore", "instrument")
        assert row.resource_id == _CANONICAL_ID
        assert row.details["instrument_id"] == _CANONICAL_ID
        assert row.details["source"] == "catalog"


def test_from_catalog_restore_opening_row_precedes_the_restore_delegate_call() -> None:
    store = InMemoryAuditStore()

    _run_catalog(store, _Delegate(store))

    assert store.events == [
        "audit:instrument.restore:applied",
        "restore",
        "audit:instrument.restore:applied",
    ]


@pytest.mark.parametrize(
    "error",
    [ArtifactIntegrityError("checksum mismatch"), ArtifactSchemaVersionMismatchError("v2")],
)
def test_from_catalog_restore_checksum_rejection_writes_terminal_failed_artifact_rejected(
    error: Exception,
) -> None:
    store = InMemoryAuditStore()

    with pytest.raises(RestoreArtifactRejectedError):
        _run_catalog(store, _Delegate(store, error=error))

    assert _statuses(store) == [
        ("applied", "started", None),
        ("failed", "failed", "artifact_rejected"),
    ]


def test_from_catalog_restore_stage_failure_writes_terminal_failed_without_traceback_or_path(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()
    boom = RuntimeError("/var/secret/path exploded at host db.internal:6379")

    with pytest.raises(RestoreStageFailedError):
        _run_catalog(store, _Delegate(store, error=boom), emitter=emitter)

    assert _statuses(store)[-1] == ("failed", "failed", "restore_failed")
    dumped = json.dumps([row.details for row in store.rows])
    for leak in ("/var/secret", "db.internal", "Traceback", "exploded"):
        assert leak not in dumped


def test_from_catalog_restore_missing_threshold_writes_terminal_failed_config_incomplete() -> None:
    store = InMemoryAuditStore()
    delegate = _Delegate(store)

    with pytest.raises(RestoreStageFailedError):
        _run_catalog(store, delegate, config=_config(threshold=None))

    assert _statuses(store) == [
        ("applied", "started", None),
        ("failed", "failed", "config_incomplete"),
    ]
    assert delegate.calls == 0


@pytest.mark.parametrize(
    "error", [AuditPostgresUnavailableError("db"), AuditPersistenceError("write")]
)
def test_from_catalog_restore_does_not_call_the_delegate_when_the_opening_row_fails(
    error: Exception,
) -> None:
    store = InMemoryAuditStore(fail_on_outcome={"applied": error})
    delegate = _Delegate(store)

    with pytest.raises(AuditTrailUnavailableError):
        _run_catalog(store, delegate)

    assert delegate.calls == 0
    assert store.rows == []


def test_from_catalog_restore_result_unchanged_when_the_terminal_row_cannot_be_written_and_it_is_logged_with_instrument_id(  # noqa: E501
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    store = InMemoryAuditStore()
    original_write = store.record_standalone
    writes = 0

    def _flaky(**kwargs: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise AuditPostgresUnavailableError("host=db.internal")
        original_write(**kwargs)  # pyright: ignore[reportArgumentType]

    store.record_standalone = _flaky  # type: ignore[method-assign]  # pyright: ignore[reportAttributeAccessIssue]

    result = _run_catalog(store, _Delegate(store), emitter=emitter)

    assert getattr(result, "instrument_id", None) == _CANONICAL_ID
    emitter.flush()
    lines = [line for line in read_lines(log_path) if line["action"] == "audit_terminal_failed"]
    (line,) = lines
    assert line["entity_id"] == _CANONICAL_ID
    assert line["run_id"]
    assert line["audit_action"] == "instrument.restore"
    assert line["reason"] == "AuditPostgresUnavailableError"
    assert "db.internal" not in str(line)


def test_failed_terminal_row_failure_keeps_the_original_restore_error() -> None:
    store = InMemoryAuditStore(fail_on_outcome={"failed": AuditPersistenceError("x")})

    with pytest.raises(RestoreArtifactRejectedError):
        _run_catalog(store, _Delegate(store, error=ArtifactIntegrityError("bad")))


def test_unknown_ambiguous_or_unreachable_catalog_writes_no_row() -> None:
    """D-G: nothing is acted on until the canonical id is resolved and the artifact fetched."""
    ambiguous = (
        _CATALOG_ENTRY,
        CuratedInstrumentEntry(
            instrument_id="cra-1.0",
            celex="32024R2847",
            title="Dup",
            source_type="external",
            jurisdiction="EU",
            short_name="CRA",
            version="1.0",
        ),
    )
    for requested, deps_kwargs, expected in (
        ("nope-9.9", {}, RestoreInstrumentIdNotFoundError),
        ("cra-1.0", {"entries": ambiguous}, RestoreInstrumentIdAmbiguousError),
        (
            "cra-1.0",
            {"catalog_error": CuratedSourceFetchError("down")},
            CuratedSourceUnavailableError,
        ),
    ):
        store = InMemoryAuditStore()
        delegate = _Delegate(store)
        deps = _catalog_deps(delegate, **deps_kwargs)  # pyright: ignore[reportArgumentType]
        with pytest.raises(expected):
            _run_catalog(store, delegate, requested_id=requested, deps=deps)
        assert store.rows == []
        assert delegate.calls == 0


def test_restore_rows_never_contain_the_source_url(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()

    _run_catalog(store, _Delegate(store))
    with pytest.raises(RestoreStageFailedError):
        _run_catalog(store, _Delegate(store, error=RuntimeError(_SOURCE_URL)), emitter=emitter)

    dumped = json.dumps([row.details for row in store.rows])
    assert "curated.internal" not in dumped
    assert "secret-path" not in dumped


# --- Slice 5: the upload path (`POST /restorations`) --------------------------------


def _upload_request(*, blob_b64: str | None = None) -> RestorationRequest:
    encoded = base64.b64encode(b'{"nodes": []}').decode("ascii")
    return RestorationRequest(
        instrument_id=_CANONICAL_ID,
        manifest=RestorationManifestPayload.model_validate(
            {
                "instrument_id": _CANONICAL_ID,
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
        ),
        baseline_blob_base64=blob_b64 or encoded,
        native_blob_base64=encoded,
    )


def _upload_deps(delegate: _Delegate) -> RestoreDependencies:
    return RestoreDependencies(
        open_db=lambda _config: cast("FalkorDB", _Db()),
        single_tenant_graph_name=lambda _config: "policy_system",
        restore=delegate,
    )


def _run_upload(
    store: InMemoryAuditStore,
    delegate: _Delegate,
    *,
    request: RestorationRequest | None = None,
    emitter: LogEmitter | None = None,
) -> object:
    return run_restoration(
        request or _upload_request(),
        config=_config(),
        actor="127.0.0.1",
        dependencies=_upload_deps(delegate),
        audit=AuditContext(_ACTOR, store),
        owner=_ACTOR,
        emitter=emitter,
    )


def test_upload_restore_writes_opening_and_terminal_with_source_upload() -> None:
    store = InMemoryAuditStore()

    _run_upload(store, _Delegate(store))

    assert _statuses(store) == [("applied", "started", None), ("applied", "succeeded", None)]
    for row in store.rows:
        assert (row.actor_subject, row.actor_issuer) == _ACTOR
        assert (row.action, row.resource_type, row.resource_id) == (
            "instrument.restore",
            "instrument",
            _CANONICAL_ID,
        )
        assert row.details["source"] == "upload"
    assert store.events == [
        "audit:instrument.restore:applied",
        "restore",
        "audit:instrument.restore:applied",
    ]


def test_upload_restore_schema_version_mismatch_writes_artifact_rejected() -> None:
    store = InMemoryAuditStore()

    with pytest.raises(RestoreArtifactRejectedError):
        _run_upload(store, _Delegate(store, error=ArtifactSchemaVersionMismatchError("v2")))

    assert _statuses(store) == [
        ("applied", "started", None),
        ("failed", "failed", "artifact_rejected"),
    ]


def test_upload_restore_does_not_run_when_opening_row_fails() -> None:
    store = InMemoryAuditStore(fail_on_outcome={"applied": AuditPostgresUnavailableError("db")})
    delegate = _Delegate(store)

    with pytest.raises(AuditTrailUnavailableError):
        _run_upload(store, delegate)

    assert delegate.calls == 0
    assert store.rows == []


def test_upload_restore_malformed_artifact_writes_no_row() -> None:
    """A body that cannot be decoded is rejected before any restore starts: nothing to audit."""
    store = InMemoryAuditStore()
    delegate = _Delegate(store)

    with pytest.raises(RestoreArtifactRejectedError):
        _run_upload(store, delegate, request=_upload_request(blob_b64="not base64!!"))

    assert store.rows == []
    assert delegate.calls == 0


def test_upload_restore_stage_failure_writes_failed_row_with_a_reason_code_only(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    store = InMemoryAuditStore()

    with pytest.raises(RestoreStageFailedError):
        _run_upload(
            store, _Delegate(store, error=RuntimeError("/var/secret leaked")), emitter=emitter
        )

    assert _statuses(store)[-1] == ("failed", "failed", "restore_failed")
    assert "/var/secret" not in json.dumps([row.details for row in store.rows])
