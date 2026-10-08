"""REST-boundary glue for ``POST /restorations`` (D5, PLAN.md §0.7).

Mirrors ``ingestion_orchestration.py``'s shape: an injection seam
(:class:`RestoreDependencies`, mirroring :class:`PipelineDependencies`) and
:func:`build_default_restore_dependencies`, which wires the real
``ps_service.restore.restore_instrument`` orchestration via a **function-local**
import so that importing ``ps_service.main`` never transitively loads
``ps_service.restore``/``ps_service.company_merge`` at module load (M6 / the
Process Harness decoupling guarantee).

:func:`run_restoration` is the thin wrapper the ``POST /restorations`` route
calls: it decodes the request body into a ``RestoreArtifact``, calls the
injected restore stage, and translates ``ps_service.restore``'s domain
exceptions into the two API-boundary error types ``error_handlers`` knows how
to shape --

* ``ArtifactIntegrityError`` / ``ArtifactSchemaVersionMismatchError`` (D9/D10,
  checksum/schema_version verification failures) -> :class:`~ps_service.api.
  errors.RestoreArtifactRejectedError` (422);
* anything else the delegate raises (``ArtifactContentRejectedError``,
  ``RestoreConcurrencyConflictError``, or an unexpected failure) ->
  :class:`~ps_service.api.errors.RestoreStageFailedError` (502), naming the
  failing stage.

No ``ps_service.restore``/``ps_service.company_merge`` type ever crosses into
this module's own runtime import graph except via the one function-local
import site (mirrors ``ingestion_orchestration.py``'s "no falkordb import
ever crosses into ps_service.api" convention for the pipeline components).
``ps_service.restore.errors``/``ps_service.restore.models`` are safe to import
at module level here -- unlike ``ps_service.restore.restore_instrument``,
neither imports ``ps_service.company_merge`` (verified: both are plain
dataclass/exception modules with no heavy dependency of their own).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from ps_service.api.error_handlers import (
    _scrub_text,  # pyright: ignore[reportPrivateUsage]  # shared scrubber; mirrors ingestion_orchestration.py's own reuse
    is_safe_verbatim,
)
from ps_service.api.errors import (
    CatalogSourceOverrideUnavailableError,
    CuratedSourceUnavailableError,
    RestoreArtifactRejectedError,
    RestoreInstrumentIdAmbiguousError,
    RestoreInstrumentIdNotFoundError,
    RestoreStageFailedError,
)
from ps_service.api.models import RestorationAcceptedResponse, RestorationStageOutcome
from ps_service.audit import AuditContext, PsycopgAuditStore
from ps_service.audit.emit import AuditTarget, record_follow_up_row, record_opening_row
from ps_service.curated_source.artifact_client import FetchArtifactCall, fetch_artifact
from ps_service.curated_source.catalog_client import FetchCatalogCall, fetch_catalog
from ps_service.curated_source.errors import (
    CuratedSourceAmbiguousInstrumentIdError,
    CuratedSourceFetchError,
    CuratedSourceUnknownInstrumentIdError,
)
from ps_service.curated_source.instrument_lookup import resolve_canonical_instrument_id
from ps_service.curated_source.resolve import EffectiveCatalogSource, resolve_effective_source
from ps_service.export.models import InstrumentManifest
from ps_service.logging.facade import emit_log_entry
from ps_service.logging.run_context import bind_run_context, current_run_id
from ps_service.restore.audit_actions import (
    INSTRUMENT_RESOURCE_TYPE,
    INSTRUMENT_RESTORE_ACTION,
    classify_restore_failure_reason,
)
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreArtifact
from ps_service.runtime_config import PsycopgRuntimeConfigStore, RuntimeConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from falkordb import (
        FalkorDB,  # pyright: ignore[reportMissingTypeStubs] -- falkordb ships no py.typed marker
    )

    from ps_service.api.models import (
        CatalogRestorationRequest,
        RestorationManifestPayload,
        RestorationRequest,
    )
    from ps_service.config import ServiceConfig
    from ps_service.logging import LogEmitter
    from ps_service.restore.models import RestoreOutcome

type RestoreAuditSource = Literal["catalog", "upload"]

_COMPONENT = "restore"
_ACTION = "restore_instrument"
_STAGE_REASON_MAX_LEN = 300
_CONFIGURATION_STAGE = "configuration"
_DEFAULT_STAGE = "restore"


class RestoreStage(Protocol):
    """Call shape of ``ps_service.restore.restore_instrument.restore_instrument``."""

    def __call__(
        self,
        artifact: RestoreArtifact,
        *,
        db: FalkorDB,
        single_tenant_graph_name: str,
        similarity_threshold: float,
        actor: str,
        emitter: LogEmitter | None = None,
        owner: tuple[str, str] | None = None,
    ) -> RestoreOutcome:
        """Restore one curated instrument's artifact end to end."""
        ...


@dataclass(frozen=True, slots=True)
class RestoreDependencies:
    """Everything :func:`run_restoration` needs that is not per-request."""

    open_db: Callable[[ServiceConfig], FalkorDB]
    single_tenant_graph_name: Callable[[ServiceConfig], str]
    restore: RestoreStage


class CatalogRestoreStage(Protocol):
    """Call shape of ``restore_instrument`` when invoked with a resolved ``source`` (D-AUDIT).

    A strict superset of :class:`RestoreStage`'s call shape (the same
    callable, ``ps_service.restore.restore_instrument.restore_instrument``,
    satisfies both Protocols structurally) -- kept separate from
    ``RestoreStage`` rather than widening it in place, so
    ``RestoreDependencies``'s existing shape (and every test constructing
    one) is untouched by issue #125 (AC-BI-005: the upload path is
    unaffected).
    """

    def __call__(
        self,
        artifact: RestoreArtifact,
        *,
        db: FalkorDB,
        single_tenant_graph_name: str,
        similarity_threshold: float,
        actor: str,
        emitter: LogEmitter | None = None,
        source: str | None = None,
        owner: tuple[str, str] | None = None,
    ) -> RestoreOutcome:
        """Restore one curated instrument's artifact end to end, recording ``source``."""
        ...


@dataclass(frozen=True, slots=True)
class CatalogRestoreDependencies:
    """Everything :func:`run_restoration_from_catalog_source` needs that is not per-request.

    Composes the curated-content fetch step (``fetch_artifact``) alongside
    the same restore-step shape :class:`RestoreDependencies` uses
    (``open_db``/``single_tenant_graph_name``/``restore``) -- declared as its
    own fields rather than nesting a :class:`RestoreDependencies` instance,
    so a test can override just the fetch step or just the restore step
    independently, mirroring every other ``*Dependencies`` bundle in this
    package.
    """

    fetch_artifact: FetchArtifactCall
    fetch_catalog: FetchCatalogCall
    resolve_effective_source: Callable[[ServiceConfig], EffectiveCatalogSource]
    open_db: Callable[[ServiceConfig], FalkorDB]
    single_tenant_graph_name: Callable[[ServiceConfig], str]
    restore: CatalogRestoreStage


@dataclass(frozen=True, slots=True)
class CatalogRestoreInfra:
    """The true infra boundaries substitutable via the restore-from-catalog factory.

    :func:`build_default_restore_from_catalog_dependencies` lets a caller substitute exactly
    these four (issue #163 Slice E; ``fetch_catalog`` added by issue #184) -- never ``restore``
    itself.

    Bundles ``fetch_artifact`` (the curated-content HTTP fetch), ``fetch_catalog`` (the
    curated-catalog-listing HTTP fetch, used to resolve a requested instrument id
    case-insensitively before the artifact fetch), ``resolve_effective_source`` (the
    runtime-config-override-then-config-fallback resolution, fail closed), and ``open_db`` (the
    FalkorDB client construction) -- mirrors ``ingestion_orchestration.GraphOpeners``'s own
    "bundle every true infra boundary a factory has under one approved accessor" shape,
    generalised here to a mixed HTTP+FalkorDB set of boundaries rather than ``GraphOpeners``'s
    pure-FalkorDB set. ``single_tenant_graph_name`` is deliberately excluded -- a pure,
    I/O-free name lookup no test has ever needed to vary, mirroring
    ``build_default_near_miss_review_dependencies``'s own precedent of leaving a
    no-substitution-need field out of its narrowed accessor entirely.
    """

    fetch_artifact: FetchArtifactCall
    fetch_catalog: FetchCatalogCall
    resolve_effective_source: Callable[[ServiceConfig], EffectiveCatalogSource]
    open_db: Callable[[ServiceConfig], FalkorDB]


# --- request decoding ---------------------------------------------------


def _to_instrument_manifest(payload: RestorationManifestPayload) -> InstrumentManifest:
    """Convert the request body's nested manifest payload to an ``InstrumentManifest``.

    Field-for-field, no renaming -- :class:`~ps_service.api.models.
    RestorationManifestPayload` is a Pydantic mirror of this dataclass's own
    fields (D1/D12).
    """
    return InstrumentManifest(
        instrument_id=payload.instrument_id,
        celex=payload.celex,
        title=payload.title,
        short_name=payload.short_name,
        version=payload.version,
        source_type=payload.source_type,
        jurisdiction=payload.jurisdiction,
        schema_version=payload.schema_version,
        exported_at=payload.exported_at,
        baseline_sha256=payload.baseline_sha256,
        native_sha256=payload.native_sha256,
    )


def _to_restore_artifact(request_body: RestorationRequest) -> RestoreArtifact:
    """Decode the request body's base64 blobs into a ``RestoreArtifact``.

    Raises:
        RestoreArtifactRejectedError: Either blob is not valid base64 -- a
            malformed artifact is rejected the same way a checksum/
            schema_version failure is (422), before any FalkorDB call.
    """
    try:
        baseline_blob = base64.b64decode(request_body.baseline_blob_base64, validate=True)
        native_blob = base64.b64decode(request_body.native_blob_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        message = f"malformed base64 artifact blob for instrument {request_body.instrument_id!r}"
        raise RestoreArtifactRejectedError(message) from exc
    return RestoreArtifact(
        manifest=_to_instrument_manifest(request_body.manifest),
        baseline_blob=baseline_blob,
        native_blob=native_blob,
    )


# --- config-completeness guard -------------------------------------------


def _require_similarity_threshold(config: ServiceConfig) -> float:
    """Return the resolved similarity threshold, or raise if it is unset.

    Restore's offline dedup replay (D6) needs the same
    ``PS_COMPANYMERGE_SIMILARITY_THRESHOLD`` value live ingestion's merge
    stage requires. Raised before the delegate is ever called -- named stage
    ``"configuration"`` since this is not a delegate failure, but the same
    502/failing-stage shape a delegate failure would produce.
    """
    threshold = config.company_merge_similarity_threshold
    if threshold is None:
        raise RestoreStageFailedError(
            stage=_CONFIGURATION_STAGE,
            reason="PS_COMPANYMERGE_SIMILARITY_THRESHOLD is not set",
        )
    return threshold


# --- failure classification -----------------------------------------------


def _classify_restore_failure(
    exc: Exception, *, instrument_id: str, actor: str, emitter: LogEmitter | None
) -> RestoreStageFailedError:
    """Classify a non-integrity/schema-version delegate failure into a ``RestoreStageFailedError``.

    Unlike ``ingestion_orchestration._classify_stage_failure``, the delegate
    (``restore_instrument``) has no per-stage granularity to report -- D8's
    whole staged-write sequence is one function, not four named stages -- so
    the stage name is derived from the exception's own class name (matched,
    not imported, so this module never needs a module-level dependency on
    ``ps_service.restore.errors``'s less-common types beyond the two already
    imported for the 422 path).

    When the exception is NOT safe to surface (so the client only sees
    ``"<stage> failed"``), its class, message and traceback are emitted
    server-side with the instrument id and caller (issue #183, AC-BI-012) --
    otherwise the real cause is unrecoverable from the service logs. The
    client message is unchanged (AC-BI-013).
    """
    exc_type_name = type(exc).__name__
    stage = {
        "ArtifactContentRejectedError": "content_validation",
        "RestoreConcurrencyConflictError": "concurrency",
    }.get(exc_type_name, _DEFAULT_STAGE)
    if is_safe_verbatim(exc):
        reason = _scrub_text(f"{exc_type_name}: {exc}")[:_STAGE_REASON_MAX_LEN]
    else:
        reason = f"{stage} failed"
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            entity_id=instrument_id,
            outcome="failed",
            extra={
                "caller": actor,
                "failing_stage": stage,
                "exception_type": exc_type_name,
                "detail": str(exc),
                "traceback": "".join(traceback.format_exception(exc)),
            },
            emitter=emitter,
        )
    return RestoreStageFailedError(stage=stage, reason=reason)


# --- response encoding -----------------------------------------------------


def _to_accepted_response(outcome: RestoreOutcome) -> RestorationAcceptedResponse:
    """Map a ``RestoreOutcome`` to the ``POST /restorations`` success body."""
    return RestorationAcceptedResponse(
        instrument_id=outcome.instrument_id,
        stages=[
            RestorationStageOutcome(stage=stage, status="succeeded") for stage in outcome.stages
        ],
    )


class _RestoreInfra(Protocol):
    """The two graph-handle accessors both dependency bundles share."""

    @property
    def open_db(self) -> Callable[[ServiceConfig], FalkorDB]:
        """Open the FalkorDB connection for the config."""
        ...

    @property
    def single_tenant_graph_name(self) -> Callable[[ServiceConfig], str]:
        """Return the single-tenant graph name for the config."""
        ...


def _perform_restore(
    *,
    instrument_id: str,
    config: ServiceConfig,
    actor: str,
    dependencies: _RestoreInfra,
    invoke: Callable[[float, FalkorDB, str], RestoreOutcome],
    emitter: LogEmitter | None,
) -> RestorationAcceptedResponse:
    """Run the restore delegate (via ``invoke``) and translate its failures to API errors.

    Shared by the upload and catalog paths (the artifact differs, the failure translation does
    not). ``invoke`` receives ``(similarity_threshold, db, single_tenant_graph_name)`` and calls
    the path's own delegate shape.
    """
    threshold = _require_similarity_threshold(config)
    db = dependencies.open_db(config)
    graph_name = dependencies.single_tenant_graph_name(config)
    try:
        outcome = invoke(threshold, db, graph_name)
    except (ArtifactIntegrityError, ArtifactSchemaVersionMismatchError) as exc:
        raise RestoreArtifactRejectedError(str(exc)) from exc
    except Exception as exc:
        raise _classify_restore_failure(
            exc, instrument_id=instrument_id, actor=actor, emitter=emitter
        ) from exc
    return _to_accepted_response(outcome)


# --- audit (issue #195) ---------------------------------------------------


def _audited_restore(
    instrument_id: str,
    *,
    source: RestoreAuditSource,
    audit: AuditContext,
    emitter: LogEmitter | None,
    restore: Callable[[], RestorationAcceptedResponse],
) -> RestorationAcceptedResponse:
    """Run ``restore`` between an ``instrument.restore`` opening row and a terminal row.

    The opening row (``applied``, ``status=started``) is FAIL-CLOSED: if it cannot be written the
    restore does not start and ``AuditTrailUnavailableError`` propagates (AC-BI-011). The terminal
    row is BEST-EFFORT: ``succeeded`` (``applied``) or ``failed`` (``failed``, enumerated
    ``reason_code``); a write failure is logged with the instrument id and run id and never changes
    the restore's result or error (AC-BI-015). ``resource_id`` is the canonical instrument id.
    A run id is bound for the call when none is, so those failure logs always carry one.
    """
    target = AuditTarget(INSTRUMENT_RESTORE_ACTION, INSTRUMENT_RESOURCE_TYPE, instrument_id)
    base = {"instrument_id": instrument_id, "source": source}
    run_scope = bind_run_context() if current_run_id() is None else contextlib.nullcontext()
    with run_scope:
        record_opening_row(
            audit,
            target,
            component=_COMPONENT,
            details={**base, "status": "started"},
            emitter=emitter,
        )
        try:
            response = restore()
        except Exception as exc:
            record_follow_up_row(
                audit,
                target,
                component=_COMPONENT,
                outcome="failed",
                details={
                    **base,
                    "status": "failed",
                    "reason_code": classify_restore_failure_reason(exc),
                },
                emitter=emitter,
            )
            raise
        record_follow_up_row(
            audit,
            target,
            component=_COMPONENT,
            outcome="applied",
            details={**base, "status": "succeeded"},
            emitter=emitter,
        )
        return response


# --- the wrapper -------------------------------------------------------


def run_restoration(
    request_body: RestorationRequest,
    *,
    config: ServiceConfig,
    actor: str,
    dependencies: RestoreDependencies,
    audit: AuditContext,
    owner: tuple[str, str] | None = None,
    emitter: LogEmitter | None = None,
) -> RestorationAcceptedResponse:
    """Restore one curated instrument's artifact via the injected delegate.

    Audit (issue #195): once the body decodes, an ``instrument.restore`` opening row
    (``source="upload"``) is written before the restore starts (fail-closed) and a terminal row
    after it (best-effort), sharing :func:`_audited_restore` with the catalog path. A body that
    cannot be decoded is rejected before any restore starts and writes no row.

    Args:
        request_body: The ``POST /restorations`` request body.
        config: The resolved service configuration.
        actor: The requesting client host (mirrors ``ingestion_orchestration
            .run_catalog_ingestion_pipeline``'s ``caller`` derivation).
        dependencies: The injected restore dependency bundle (the production
            bundle in production; a fake in fast tests).
        owner: The restoring caller's verified ``(sub, iss)`` pair (issue #183),
            which becomes the owner of any imported draft Policy; ``None`` when
            no verified identity exists, in which case an artifact carrying
            Policy content is refused.
        audit: Who is acting and where the ``instrument.restore`` audit rows go.
        emitter: Optional log emitter for the server-side failure-detail entry.

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.

    Raises:
        AuditTrailUnavailableError: The opening audit row could not be written; nothing was
            restored.
        RestoreArtifactRejectedError: The artifact is malformed, or fails
            checksum (D9) / schema_version (D10) verification (422).
        RestoreStageFailedError: Any other failure, including a missing
            similarity-threshold configuration value (502).
    """
    artifact = _to_restore_artifact(request_body)

    def _restore() -> RestorationAcceptedResponse:
        return _perform_restore(
            instrument_id=request_body.instrument_id,
            config=config,
            actor=actor,
            dependencies=dependencies,
            invoke=lambda threshold, db, graph_name: dependencies.restore(
                artifact,
                db=db,
                single_tenant_graph_name=graph_name,
                similarity_threshold=threshold,
                actor=actor,
                owner=owner,
            ),
            emitter=emitter,
        )

    return _audited_restore(
        request_body.instrument_id,
        source="upload",
        audit=audit,
        emitter=emitter,
        restore=_restore,
    )


def run_restoration_from_catalog_source(
    request_body: CatalogRestorationRequest,
    *,
    config: ServiceConfig,
    actor: str,
    dependencies: CatalogRestoreDependencies,
    audit: AuditContext,
    owner: tuple[str, str] | None = None,
    emitter: LogEmitter | None = None,
) -> RestorationAcceptedResponse:
    """Fetch and restore one curated instrument's artifact from the curated-content source.

    Issue #125, ``POST /restorations/from-catalog`` (D-NEW-ROUTE) --
    restores via the same delegate :func:`run_restoration` (the upload
    path) uses. Since Slice 3, the source fetched from is the *effective*
    curated-content source: a persisted override (a ``runtime_config`` row, issue #130) when
    one exists, else ``config.curated_source_base_url``, resolved on every call via
    ``dependencies.resolve_effective_source`` (AC-BI-013) before
    ``dependencies.fetch_artifact`` is called -- an override read failure fails the request
    closed (``CatalogSourceOverrideUnavailableError``, AC-BI-010) rather than falling back
    to ``config.curated_source_base_url``. Before the artifact fetch, ``request_body.
    instrument_id`` is resolved case-insensitively against ``dependencies.fetch_catalog``'s
    own listing (issue #184, AC-BI-001) via :func:`~ps_service.curated_source.
    instrument_lookup.resolve_canonical_instrument_id`, and the canonical (catalog-cased) id
    is used for both the artifact fetch and every downstream audit entry (AC-BI-006) -- a
    ``catalog.json``-fetch failure surfaces through the same ``CuratedSourceUnavailableError``
    path as an artifact-fetch failure always has (AC-BI-005). The fetched artifact is passed to the
    injected ``restore`` delegate unmodified -- the exact same D9 checksum /
    D10 schema_version verification :func:`run_restoration` relies on runs
    first and unconditionally inside that one shared delegate (never
    duplicated here), so a fetched-but-corrupted or version-mismatched
    artifact is rejected exactly like an uploaded one (AC-BI-007/009, mirrors
    #66). The resolved effective source URL is passed through as ``source``
    so the restore's own audit log entries carry it (AC-BI-011, mirrors #66
    AC-BI-016) -- distinct from :func:`run_restoration`, whose call site
    never passes ``source`` at all, keeping the upload path's audit log shape
    byte-identical (D-AUDIT).

    Args:
        request_body: The ``POST /restorations/from-catalog`` request body
            (just the instrument id to fetch).
        config: The resolved service configuration -- names the effective
            curated-content source URL to fetch from.
        actor: The requesting client host (mirrors ``run_restoration``'s own
            ``actor`` derivation).
        dependencies: The injected fetch-and-restore dependency bundle (the
            production bundle in production; a fake in fast tests).
        owner: The restoring caller's verified ``(sub, iss)`` pair (issue #183),
            the owner of any imported draft Policy; ``None`` when no verified
            identity exists.
        audit: Who is acting and where the ``instrument.restore`` audit rows go (issue #195).
            After the canonical id is resolved and the artifact fetched, an opening row is
            written (fail-closed) before the restore starts and a terminal row after it
            (best-effort); an unknown, ambiguous or unfetchable id writes no row.
        emitter: Optional log emitter for the server-side failure-detail entry.

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.

    Raises:
        AuditTrailUnavailableError: The opening audit row could not be written; nothing was
            restored (issue #195).
        CatalogSourceOverrideUnavailableError: The override could not be read (AC-BI-010) --
            503, nothing fetched or restored.
        CuratedSourceUnavailableError: The configured source is unreachable,
            or the fetched artifact is missing/malformed (AC-BI-004/006) --
            502, naming the source and instrument.
        RestoreInstrumentIdAmbiguousError: ``request_body.instrument_id`` matches more
            than one catalog entry case-insensitively (issue #184, AC-BI-003) -- 409,
            naming the requested id and every colliding canonical id; nothing is
            fetched.
        RestoreInstrumentIdNotFoundError: ``request_body.instrument_id`` matches no
            catalog entry in any case (issue #184, AC-BI-004) -- 404, naming the
            requested id and the closest candidate ids; nothing is fetched.
        RestoreArtifactRejectedError: The fetched artifact fails checksum
            (D9) / schema_version (D10) verification (422, AC-BI-007/009).
        RestoreStageFailedError: Any other restore failure, including a
            missing similarity-threshold configuration value (502).
    """
    try:
        effective_source = dependencies.resolve_effective_source(config)
    except RuntimeConfigError as exc:
        raise CatalogSourceOverrideUnavailableError from exc
    try:
        catalog_entries = dependencies.fetch_catalog(effective_source.url)
        canonical_id = resolve_canonical_instrument_id(catalog_entries, request_body.instrument_id)
        fetched = dependencies.fetch_artifact(effective_source.url, canonical_id)
    except CuratedSourceFetchError as exc:
        raise CuratedSourceUnavailableError(str(exc)) from exc
    except CuratedSourceAmbiguousInstrumentIdError as exc:
        raise RestoreInstrumentIdAmbiguousError(str(exc)) from exc
    except CuratedSourceUnknownInstrumentIdError as exc:
        raise RestoreInstrumentIdNotFoundError(str(exc)) from exc
    artifact = RestoreArtifact(
        manifest=fetched.manifest,
        baseline_blob=fetched.baseline_blob,
        native_blob=fetched.native_blob,
    )

    def _restore() -> RestorationAcceptedResponse:
        return _perform_restore(
            instrument_id=canonical_id,
            config=config,
            actor=actor,
            dependencies=dependencies,
            invoke=lambda threshold, db, graph_name: dependencies.restore(
                artifact,
                db=db,
                single_tenant_graph_name=graph_name,
                similarity_threshold=threshold,
                actor=actor,
                source=effective_source.url,
                owner=owner,
            ),
            emitter=emitter,
        )

    return _audited_restore(
        canonical_id, source="catalog", audit=audit, emitter=emitter, restore=_restore
    )


# --- default wiring (M6 -- every restore/company_merge import below is function-local) ---


def _default_open_db(config: ServiceConfig) -> FalkorDB:
    """Open the real FalkorDB connection for ``config``."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
    )

    return connect_from_config(config)


def _default_single_tenant_graph_name(config: ServiceConfig) -> str:
    """Return the single-tenant (``policy_system``) graph name."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- M6: function-local keeps ps_service.main off Company Merge at import
        single_tenant_graph_name,
    )

    _ = config
    return single_tenant_graph_name()


def build_default_restore_dependencies() -> RestoreDependencies:
    """Wire the real ``restore_instrument`` orchestration into a ``RestoreDependencies``.

    ``restore_instrument`` is imported **function-locally** (here, and in the
    opener helpers above) so that importing ``ps_service.main`` never
    transitively loads ``ps_service.restore``/``ps_service.company_merge`` at
    module load (M6 / the Process Harness decoupling guarantee) -- mirrors
    ``ingestion_orchestration.build_default_pipeline_dependencies`` exactly.

    Returns:
        A :class:`RestoreDependencies` bound to the production restore
        orchestration and the real FalkorDB connection/graph-name helpers.
    """
    from ps_service.restore.restore_instrument import (  # noqa: PLC0415 -- M6: function-local
        restore_instrument,
    )

    return RestoreDependencies(
        open_db=_default_open_db,
        single_tenant_graph_name=_default_single_tenant_graph_name,
        restore=restore_instrument,
    )


def _default_resolve_effective_source(config: ServiceConfig) -> EffectiveCatalogSource:
    """Resolve the effective curated-content source (issue #125, AC-BI-013; #130, AC-BI-010).

    Reads the override from the PS state Postgres's ``runtime_config`` table -- no FalkorDB
    involved. A failed read raises (fail closed) rather than falling back to the default.
    """
    store = PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config))
    return resolve_effective_source(config, store=store)


def build_default_restore_from_catalog_infra() -> CatalogRestoreInfra:
    """Return the real artifact fetch, catalog fetch, source resolution, and FalkorDB opener.

    The *only* moving parts :func:`build_default_restore_from_catalog_dependencies` lets a
    caller substitute (issue #163 Slice E; ``fetch_catalog`` added by issue #184) -- four true
    infra boundaries, never the real ``restore_instrument`` business logic sitting on top of
    them. Extracted to its own
    top-level function (rather than inlined in
    :func:`build_default_restore_from_catalog_dependencies`) specifically so it is its own,
    independently addressable module-level name: a caller-side ``monkeypatch.setattr(
    "ps_service.api.restore_orchestration.build_default_restore_from_catalog_infra", ...)``
    substitutes the fetch/resolve/open_db boundary alone, while
    :func:`build_default_restore_from_catalog_dependencies` itself -- called with no
    arguments, exactly as every production caller (`mcp_server.py`'s one call site) already
    does -- still resolves this name at call time (ordinary Python late-binding for a bare
    module-level call, mirroring ``ingestion_orchestration.build_default_graph_openers``'s/
    ``near_miss_review_orchestration.build_default_near_miss_review_graph_opener``'s own
    issue #163 Slice C/D precedent) and so picks up the substitution automatically, with zero
    change to its own call sites. ``docs/coding-standards/approved-mock-boundaries.yaml``
    lists this function itself as the approved boundary -- not
    ``build_default_restore_from_catalog_dependencies``, which stays off that list since it
    still bundles real business logic (``restore``) alongside this boundary.

    Returns:
        A :class:`CatalogRestoreInfra` bound to the production curated-content fetch step,
        the production curated-catalog fetch step, the production `resolve_effective_source`
        (issue #125, Slice 3), and the production FalkorDB connection opener.
    """
    return CatalogRestoreInfra(
        fetch_artifact=fetch_artifact,
        fetch_catalog=fetch_catalog,
        resolve_effective_source=_default_resolve_effective_source,
        open_db=_default_open_db,
    )


def build_default_restore_from_catalog_dependencies(
    *, infra: CatalogRestoreInfra | None = None
) -> CatalogRestoreDependencies:
    """Wire the real fetch-and-restore path into a ``CatalogRestoreDependencies``.

    ``restore_instrument`` is imported **function-locally**, exactly like
    :func:`build_default_restore_dependencies` (M6 -- ``ps_service.main``
    never transitively loads ``ps_service.restore``/``ps_service.
    company_merge`` at module load).

    issue #163 Slice E narrowed this factory's own DI seam: ``infra`` is the *only*
    substitutable parameter. There is deliberately no ``restore`` parameter any more --
    ``restore`` is always the real, shipped ``ps_service.restore.restore_instrument.
    restore_instrument``, unconditionally, with no way for a caller (test or otherwise) to
    substitute fake business logic through this function's own signature. Before this
    change, a caller could -- and every ``mcp_interface`` unit test covering this tool did --
    replace this factory's *entire* return value wholesale, faking real restore business
    logic in the name of substituting only the fetch/FalkorDB boundary beneath it (the
    "delegate, don't reimplement" violation L2's MCP Interface Patterns section warns
    against; see ``.orchestrator/tracker/issue-163/AUDIT_RAW/mcp_interface_part2.md``'s
    DOMINANT FINDING). ``infra=None`` (the default -- every production caller, unchanged)
    resolves :func:`build_default_restore_from_catalog_infra` at call time, so a
    ``monkeypatch.setattr`` of *that* function (see its own docstring) is picked up
    automatically even though this factory itself is never patched.

    Args:
        infra: The fetch/effective-source/FalkorDB boundary bundle to use, or ``None``
            (every production caller) to use the real one.

    Returns:
        A :class:`CatalogRestoreDependencies` bound to the production restore entry point,
        the production graph-name helper, and the given (or real) infra bundle.
    """
    from ps_service.restore.restore_instrument import (  # noqa: PLC0415 -- M6: function-local
        restore_instrument,
    )

    resolved_infra = infra or build_default_restore_from_catalog_infra()
    return CatalogRestoreDependencies(
        fetch_artifact=resolved_infra.fetch_artifact,
        fetch_catalog=resolved_infra.fetch_catalog,
        resolve_effective_source=resolved_infra.resolve_effective_source,
        open_db=resolved_infra.open_db,
        single_tenant_graph_name=_default_single_tenant_graph_name,
        restore=restore_instrument,
    )
