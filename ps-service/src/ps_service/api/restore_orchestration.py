"""REST-boundary glue for restoring curated instruments.

Serves ``POST /restorations`` and ``POST /restorations/from-catalog``. Two entry points share
one failure translation. :func:`run_restoration` restores an uploaded artifact;
:func:`run_restoration_from_catalog_source` fetches the artifact from the curated-content source
first. Each decodes or fetches the artifact, runs the injected restore routine between an
``instrument.restore`` opening audit row and a terminal row, and translates the routine's domain
errors into the two API errors the error handlers know how to shape:

* a checksum or schema-version verification failure becomes ``RestoreArtifactRejectedError``
  (422);
* any other failure becomes ``RestoreStageFailedError`` (502) naming the failing stage. A missing
  Graph Write Gateway is not translated: ``GraphWriteUnavailableError`` (503) passes through.

The dependency bundles (:class:`RestoreDependencies`, :class:`CatalogRestoreDependencies`) are the
injection seam, and the ``build_default_*`` factories wire production. The factories import the
restore routine and Company Merge inside the function, so importing the application module never
loads them; they load on first use at request time.
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
    GraphWriteUnavailableError,
    RestoreArtifactRejectedError,
    RestoreInstrumentIdAmbiguousError,
    RestoreInstrumentIdNotFoundError,
    RestoreStageFailedError,
)
from ps_service.api.graph_write_access import graph_write_access
from ps_service.api.models import RestorationAcceptedResponse, RestorationStageOutcome
from ps_service.audit import AuditContext, PsycopgAuditStore
from ps_service.audit.emit import (
    AuditTarget,
    record_follow_up_row,
    record_opening_row_with_event_id,
)
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
    classify_restore_terminal_row,
)
from ps_service.restore.errors import ArtifactIntegrityError, ArtifactSchemaVersionMismatchError
from ps_service.restore.models import RestoreArtifact
from ps_service.runtime_config import PsycopgRuntimeConfigStore, RuntimeConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from ps_service.api.graph_write_access import GraphWriteAccess
    from ps_service.api.models import (
        CatalogRestorationRequest,
        RestorationManifestPayload,
        RestorationRequest,
    )
    from ps_service.config import ServiceConfig
    from ps_service.graph_gateway.read_only import ReadOnlyDatabase
    from ps_service.logging import LogEmitter
    from ps_service.restore.models import RestoreOutcome

type RestoreAuditSource = Literal["catalog", "upload"]

_COMPONENT = "restore"
_ACTION = "restore_instrument"
_STAGE_REASON_MAX_LEN = 300
_CONFIGURATION_STAGE = "configuration"
_DEFAULT_STAGE = "restore"
_GRAPH_WRITE_STAGE = "graph_write"


class RestoreStage(Protocol):
    """Callable contract for the restore routine that ``POST /restorations`` runs.

    Production binds it to ``restore_instrument``; tests substitute a fake. The keyword-only call
    shape is fixed so the wrapper can pass the same arguments to either.
    """

    def __call__(  # noqa: PLR0913 -- mirrors restore_instrument's keyword-only call shape
        self,
        artifact: RestoreArtifact,
        *,
        db: ReadOnlyDatabase,
        single_tenant_graph_name: str,
        similarity_threshold: float,
        actor: str,
        open_gateway: Callable[[], object],
        audit_event_id: str,
        emitter: LogEmitter | None = None,
        owner: tuple[str, str] | None = None,
    ) -> RestoreOutcome:
        """Restore one curated instrument's artifact end to end.

        Args:
            artifact: The decoded artifact: manifest and the baseline and native blobs.
            db: Read-only handle on the graph database, used to read the single-tenant graph.
            single_tenant_graph_name: Name of the single-tenant (merged) graph; non-empty.
            similarity_threshold: Minimum Capability embedding similarity for convergence, in 0 to
                1.
            actor: The requesting client host, recorded as the caller in audit log entries.
            open_gateway: Returns the Graph Write Gateway, or raises when the service has none. The
                routine calls it only after the artifact verified.
            audit_event_id: Id of the opening ``instrument.restore`` audit row; every log group of
                the restore carries it.
            emitter: Log emitter, or ``None`` for the process default.
            owner: The restoring caller's verified ``(sub, iss)`` pair, or ``None`` when there is
                none.

        Returns:
            A ``RestoreOutcome`` with the instrument id and the stages that ran. A failed restore
            raises instead of returning.

        Raises:
            ArtifactIntegrityError: A blob does not match its manifest checksum.
            ArtifactSchemaVersionMismatchError: The manifest schema version is not the one this
                service supports.
            ArtifactContentRejectedError: A graph in the artifact violates its allow-list or cannot
                be stored.
            RestoreOwnerRequiredError: The artifact carries Policy content and ``owner`` is
                ``None``.
            RestoreConcurrencyConflictError: The single-tenant graph kept changing on every attempt.
            RestoreGroupRejectedError: The gateway refused the write group; nothing was logged.
            RestoreGraphWriteFailedError: The write log or the graph database failed; the restore
                did not complete.
            RestoreApplyPendingError: The write was committed to the log and is applied on recovery.
            GraphWriteUnavailableError: ``open_gateway`` found no Graph Write Gateway.

        Side Effects:
            Emits ``started`` and then ``succeeded`` or ``failed`` log entries, reads the
            single-tenant graph, and on success replaces the instrument's native and baseline graphs
            and merges the single-tenant graph through the gateway as one logged group.
        """
        ...


@dataclass(frozen=True, slots=True)
class RestoreDependencies:
    """Per-process collaborators of :func:`run_restoration`, injectable for tests.

    Attributes:
        open_db: Opens the read-only graph database handle for a service configuration.
        single_tenant_graph_name: Returns the single-tenant graph name for a service configuration.
        restore: The restore routine that verifies and writes the artifact.
        graph_write_access: Returns the Graph Write Gateway access for a service configuration; it
            raises ``GraphWriteUnavailableError`` when the service has none.
    """

    open_db: Callable[[ServiceConfig], ReadOnlyDatabase]
    single_tenant_graph_name: Callable[[ServiceConfig], str]
    restore: RestoreStage
    graph_write_access: Callable[[ServiceConfig], GraphWriteAccess]


class CatalogRestoreStage(Protocol):
    """Callable contract for the restore routine on the restore-from-catalog path.

    The same routine as :class:`RestoreStage` with one extra optional ``source`` argument that
    records which curated-content source the artifact came from. It is a separate contract so the
    upload path does not have to accept ``source``.
    """

    def __call__(  # noqa: PLR0913 -- mirrors restore_instrument's keyword-only call shape
        self,
        artifact: RestoreArtifact,
        *,
        db: ReadOnlyDatabase,
        single_tenant_graph_name: str,
        similarity_threshold: float,
        actor: str,
        open_gateway: Callable[[], object],
        audit_event_id: str,
        emitter: LogEmitter | None = None,
        source: str | None = None,
        owner: tuple[str, str] | None = None,
    ) -> RestoreOutcome:
        """Restore one curated instrument's artifact end to end, recording its ``source``.

        Args:
            artifact: The artifact fetched from the curated-content source.
            db: Read-only handle on the graph database, used to read the single-tenant graph.
            single_tenant_graph_name: Name of the single-tenant (merged) graph; non-empty.
            similarity_threshold: Minimum Capability embedding similarity for convergence, in 0 to
                1.
            actor: The requesting client host (REST) or the fixed MCP caller label, recorded as the
                caller in audit log entries.
            open_gateway: Returns the Graph Write Gateway, or raises when the service has none. The
                routine calls it only after the artifact verified.
            audit_event_id: Id of the opening ``instrument.restore`` audit row; every log group of
                the restore carries it.
            emitter: Log emitter, or ``None`` for the process default.
            source: Base URL of the curated-content source, added to every audit log entry; ``None``
                adds none.
            owner: The restoring caller's verified ``(sub, iss)`` pair, or ``None`` when there is
                none.

        Returns:
            A ``RestoreOutcome`` with the instrument id and the stages that ran. A failed restore
            raises instead of returning.

        Raises:
            ArtifactIntegrityError: A blob does not match its manifest checksum.
            ArtifactSchemaVersionMismatchError: The manifest schema version is not the one this
                service supports.
            ArtifactContentRejectedError: A graph in the artifact violates its allow-list or cannot
                be stored.
            RestoreOwnerRequiredError: The artifact carries Policy content and ``owner`` is
                ``None``.
            RestoreConcurrencyConflictError: The single-tenant graph kept changing on every attempt.
            RestoreGroupRejectedError: The gateway refused the write group; nothing was logged.
            RestoreGraphWriteFailedError: The write log or the graph database failed; the restore
                did not complete.
            RestoreApplyPendingError: The write was committed to the log and is applied on recovery.
            GraphWriteUnavailableError: ``open_gateway`` found no Graph Write Gateway.

        Side Effects:
            Emits ``started`` and then ``succeeded`` or ``failed`` log entries, reads the
            single-tenant graph, and on success replaces the instrument's native and baseline graphs
            and merges the single-tenant graph through the gateway as one logged group.
        """
        ...


@dataclass(frozen=True, slots=True)
class CatalogRestoreDependencies:
    """Per-process collaborators of :func:`run_restoration_from_catalog_source`.

    The fetch steps sit beside the same restore-step fields :class:`RestoreDependencies` holds. They
    are separate fields, not a nested bundle, so a test can replace the fetch step or the restore
    step on its own.

    Attributes:
        fetch_artifact: Fetches an instrument's manifest and blobs from a curated-content source.
        fetch_catalog: Fetches the source's catalog listing, used to resolve the requested id.
        resolve_effective_source: Returns the curated-content source to use for a configuration: the
            persisted override when one exists, else the configured default. It raises
            ``RuntimeConfigError`` when the override cannot be read.
        open_db: Opens the read-only graph database handle for a service configuration.
        single_tenant_graph_name: Returns the single-tenant graph name for a service configuration.
        restore: The restore routine that verifies and writes the artifact.
        graph_write_access: Returns the Graph Write Gateway access for a service configuration; it
            raises ``GraphWriteUnavailableError`` when the service has none.
    """

    fetch_artifact: FetchArtifactCall
    fetch_catalog: FetchCatalogCall
    resolve_effective_source: Callable[[ServiceConfig], EffectiveCatalogSource]
    open_db: Callable[[ServiceConfig], ReadOnlyDatabase]
    single_tenant_graph_name: Callable[[ServiceConfig], str]
    restore: CatalogRestoreStage
    graph_write_access: Callable[[ServiceConfig], GraphWriteAccess]


@dataclass(frozen=True, slots=True)
class CatalogRestoreInfra:
    """The external boundaries a caller may substitute when building catalog-restore dependencies.

    :func:`build_default_restore_from_catalog_dependencies` accepts exactly these four, and never
    the restore routine itself, so a substitution replaces I/O without replacing restore logic. The
    single-tenant graph name is left out because it is a pure lookup with no I/O.

    Attributes:
        fetch_artifact: Fetches an instrument's manifest and blobs over HTTP from a curated-content
            source.
        fetch_catalog: Fetches the source's catalog listing over HTTP, used to resolve the requested
            instrument id case-insensitively before the artifact fetch.
        resolve_effective_source: Resolves the persisted source override, else the configured
            default. It fails closed when the override cannot be read.
        open_db: Opens the read-only graph database handle for a service configuration.
    """

    fetch_artifact: FetchArtifactCall
    fetch_catalog: FetchCatalogCall
    resolve_effective_source: Callable[[ServiceConfig], EffectiveCatalogSource]
    open_db: Callable[[ServiceConfig], ReadOnlyDatabase]


# --- request decoding ---------------------------------------------------


def _to_instrument_manifest(payload: RestorationManifestPayload) -> InstrumentManifest:
    """Copy the request body's manifest payload field for field into an ``InstrumentManifest``."""
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
        RestoreArtifactRejectedError: Either blob is not valid base64. The artifact is rejected like
            a checksum or schema-version failure (422), before any graph call.
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
    """Return the configured Company Merge similarity threshold, or fail if it is unset.

    The restore replays convergence with the same threshold that live ingestion uses. The failure is
    reported as stage ``configuration`` and is raised before the restore routine runs.

    Raises:
        RestoreStageFailedError: The threshold setting is missing (502, stage ``configuration``).
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
    """Turn an unexpected restore failure into a ``RestoreStageFailedError`` naming its stage.

    The restore routine is one step, so the stage is derived from the exception's class name:
    ``content_validation``, ``concurrency``, ``graph_write``, or ``restore`` for any other class.
    When the exception is safe to show, the client reason is its scrubbed ``"<Type>: <message>"``
    text, truncated to a fixed length. Otherwise the client reason is only ``"<stage> failed"``, and
    the exception's class, message and traceback are logged server side with the instrument id and
    caller so the real cause stays recoverable.

    Args:
        exc: The exception raised by the restore routine.
        instrument_id: The instrument being restored, recorded on the failure log entry.
        actor: The requesting client host (REST) or the fixed MCP caller label, recorded as the
            caller on the failure log entry.
        emitter: Log emitter, or ``None`` for the process default.

    Returns:
        The error to raise, carrying the stage and the client-safe reason.

    Side Effects:
        Emits one ``failed`` log entry when the exception is not safe to show.
    """
    exc_type_name = type(exc).__name__
    stage = {
        "ArtifactContentRejectedError": "content_validation",
        "RestoreConcurrencyConflictError": "concurrency",
        "RestoreGroupRejectedError": _GRAPH_WRITE_STAGE,
        "RestoreApplyPendingError": _GRAPH_WRITE_STAGE,
        "RestoreGraphWriteFailedError": _GRAPH_WRITE_STAGE,
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
    """Build the restore success body, one ``succeeded`` entry per stage the restore ran."""
    return RestorationAcceptedResponse(
        instrument_id=outcome.instrument_id,
        stages=[
            RestorationStageOutcome(stage=stage, status="succeeded") for stage in outcome.stages
        ],
        near_miss_reviews_created=outcome.near_miss_reviews_created,
    )


class _RestoreInfra(Protocol):
    """The graph-handle accessors shared by both restore dependency bundles."""

    @property
    def open_db(self) -> Callable[[ServiceConfig], ReadOnlyDatabase]:
        """Open the read-only graph database handle for the configuration."""
        ...

    @property
    def single_tenant_graph_name(self) -> Callable[[ServiceConfig], str]:
        """Return the single-tenant graph name for the configuration."""
        ...

    @property
    def graph_write_access(self) -> Callable[[ServiceConfig], GraphWriteAccess]:
        """Return the Graph Write Gateway access for the configuration; raise if there is none."""
        ...


def _perform_restore(
    *,
    instrument_id: str,
    config: ServiceConfig,
    actor: str,
    dependencies: _RestoreInfra,
    audit_event_id: str,
    invoke: Callable[[float, ReadOnlyDatabase, str, Callable[[], object], str], RestoreOutcome],
    emitter: LogEmitter | None,
) -> RestorationAcceptedResponse:
    """Run the restore routine through ``invoke`` and translate its failures into API errors.

    Shared by the upload and catalog paths: the artifact differs, the failure translation does not.
    ``invoke`` receives ``(similarity_threshold, db, single_tenant_graph_name, open_gateway,
    audit_event_id)`` and calls the path's own restore routine. ``open_gateway`` looks up the
    gateway lazily, inside the routine and after the artifact verified, so a corrupt artifact is
    still rejected with 422 when no gateway is installed.

    Args:
        instrument_id: The instrument being restored, used in failure logging.
        config: The resolved service configuration.
        actor: The requesting client host (REST) or the fixed MCP caller label.
        dependencies: Source of the database handle, graph name and gateway access.
        audit_event_id: Id of the opening audit row, handed to the restore routine.
        invoke: Calls the restore routine with the values above.
        emitter: Log emitter, or ``None`` for the process default.

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.

    Raises:
        RestoreStageFailedError: The similarity threshold is unset, or the routine failed for any
            reason other than those below (502).
        RestoreArtifactRejectedError: The artifact fails checksum or schema-version verification
            (422).
        GraphWriteUnavailableError: The gateway lookup found no gateway (503); passed through
            unchanged.
        Exception: An error from opening the database handle or resolving the graph name propagates
            unchanged.

    Side Effects:
        Opens the database handle, runs the restore routine (graph writes through the gateway), and
        emits a failure log entry for an unsafe-to-show error.
    """
    threshold = _require_similarity_threshold(config)
    db = dependencies.open_db(config)
    graph_name = dependencies.single_tenant_graph_name(config)

    def open_gateway() -> object:
        return dependencies.graph_write_access(config).gateway

    try:
        outcome = invoke(threshold, db, graph_name, open_gateway, audit_event_id)
    except (ArtifactIntegrityError, ArtifactSchemaVersionMismatchError) as exc:
        raise RestoreArtifactRejectedError(str(exc)) from exc
    except GraphWriteUnavailableError:
        raise
    except Exception as exc:
        raise _classify_restore_failure(
            exc, instrument_id=instrument_id, actor=actor, emitter=emitter
        ) from exc
    return _to_accepted_response(outcome)


# --- audit rows -----------------------------------------------------------


def _audited_restore(
    instrument_id: str,
    *,
    source: RestoreAuditSource,
    audit: AuditContext,
    emitter: LogEmitter | None,
    restore: Callable[[str], RestorationAcceptedResponse],
) -> RestorationAcceptedResponse:
    """Run ``restore`` between an ``instrument.restore`` opening audit row and a terminal row.

    The opening row (``applied``, status ``started``) is fail-closed: when it cannot be written the
    restore does not start. Its event id is handed to ``restore`` and anchors every log group of the
    restore. The terminal row is best effort: ``succeeded`` (outcome ``applied``), ``rejected`` (the
    gateway refused the group), ``committed_apply_pending`` (outcome ``applied``; committed to the
    graph log and applied on recovery) or ``failed`` with an enumerated reason code. A terminal-row
    write failure is logged with the instrument and run id and never changes the restore's result or
    error. A run id is bound for the call when none is bound, so those log entries always carry one.

    Args:
        instrument_id: The canonical instrument id; the audit rows' resource id.
        source: Where the artifact came from, ``"catalog"`` or ``"upload"``; recorded in the
            details.
        audit: The acting principal and audit store the rows are written to.
        emitter: Log emitter, or ``None`` for the process default.
        restore: Runs the restore, given the opening row's event id.

    Returns:
        The response ``restore`` returned.

    Raises:
        AuditTrailUnavailableError: The opening row could not be written; ``restore`` did not run.
        Exception: Any error ``restore`` raises propagates unchanged after the terminal row is
            attempted.

    Side Effects:
        Writes the opening and terminal audit rows and binds a run id for the call when none is
        bound.
    """
    target = AuditTarget(INSTRUMENT_RESTORE_ACTION, INSTRUMENT_RESOURCE_TYPE, instrument_id)
    base = {"instrument_id": instrument_id, "source": source}
    run_scope = bind_run_context() if current_run_id() is None else contextlib.nullcontext()
    with run_scope:
        audit_event_id = record_opening_row_with_event_id(
            audit,
            target,
            component=_COMPONENT,
            details={**base, "status": "started"},
            emitter=emitter,
        )
        try:
            response = restore(audit_event_id)
        except Exception as exc:
            terminal = classify_restore_terminal_row(exc)
            details: dict[str, object] = {**base, "status": terminal.status}
            if terminal.reason_code is not None:
                details["reason_code"] = terminal.reason_code
            record_follow_up_row(
                audit,
                target,
                component=_COMPONENT,
                outcome=terminal.outcome,
                details=details,
                emitter=emitter,
            )
            raise
        record_follow_up_row(
            audit,
            target,
            component=_COMPONENT,
            outcome="applied",
            details={
                **base,
                "status": "succeeded",
                "near_miss_reviews_created": response.near_miss_reviews_created,
            },
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
    """Restore one curated instrument from an uploaded artifact, with audit rows.

    Decodes the request body into an artifact, then runs the injected restore routine between an
    ``instrument.restore`` opening audit row (``source="upload"``) and a terminal row. The opening
    row is fail-closed and the terminal row best effort. A body that cannot be decoded is rejected
    before any audit row is written. Failures from the routine are translated into API errors.

    Args:
        request_body: The upload request: instrument id, manifest, and base64-encoded baseline and
            native blobs.
        config: The resolved service configuration.
        actor: The requesting client host, recorded as the caller.
        dependencies: The restore collaborators (database opener, graph-name lookup, restore
            routine, gateway access).
        audit: The acting principal and audit store the ``instrument.restore`` rows are written to.
        owner: The restoring caller's verified ``(sub, iss)`` pair. It owns any imported draft
            Policy; ``None`` when there is no verified caller, in which case an artifact with Policy
            content is refused.
        emitter: Log emitter for the server-side failure entry, or ``None`` for the process default.

    Returns:
        A :class:`RestorationAcceptedResponse` naming the instrument and the completed stages.

    Raises:
        AuditTrailUnavailableError: The opening audit row could not be written; nothing was
            restored.
        RestoreArtifactRejectedError: A blob is not valid base64, or the artifact fails checksum or
            schema-version verification (422).
        GraphWriteUnavailableError: The artifact verified but the service has no Graph Write Gateway
            (503); nothing was written. The terminal audit row records a failure.
        RestoreStageFailedError: Any other failure, including an unset similarity-threshold setting
            (502). A gateway refusal, an unreachable store or a commit not yet applied is stage
            ``graph_write``.

    Side Effects:
        Writes the opening and terminal ``instrument.restore`` audit rows and, on success, replaces
        the instrument's graphs through the Graph Write Gateway. Emits log entries.
    """
    artifact = _to_restore_artifact(request_body)

    def _restore(audit_event_id: str) -> RestorationAcceptedResponse:
        return _perform_restore(
            instrument_id=request_body.instrument_id,
            config=config,
            actor=actor,
            dependencies=dependencies,
            audit_event_id=audit_event_id,
            invoke=lambda threshold, db, graph_name, open_gateway, event_id: dependencies.restore(
                artifact,
                db=db,
                single_tenant_graph_name=graph_name,
                similarity_threshold=threshold,
                actor=actor,
                open_gateway=open_gateway,
                audit_event_id=event_id,
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
    """Fetch one curated instrument's artifact from the curated-content source and restore it.

    The source is the effective one: the persisted override when it exists, else
    ``config.curated_source_base_url``, resolved on every call. An unreadable override fails the
    request closed rather than falling back to the default. The requested id is matched
    case-insensitively against the source's catalog listing, and the canonical (catalog-cased) id is
    used for the artifact fetch and for every audit row. The fetched artifact is handed to the same
    restore routine as an upload, which verifies checksums and schema version first, so a corrupted
    or version-mismatched artifact is rejected exactly like an uploaded one. The source URL is
    passed to the routine so its log entries record it.

    An unknown, ambiguous or unfetchable id writes no audit row. Once the artifact is fetched, an
    ``instrument.restore`` opening row (``source="catalog"``) is written before the restore starts
    (fail-closed) and a terminal row after it (best effort).

    Args:
        request_body: The request: the instrument id to fetch, in any letter case.
        config: The resolved service configuration; supplies the default source URL.
        actor: The requesting client host (REST) or the fixed MCP caller label, recorded as the
            caller.
        dependencies: The fetch-and-restore collaborators.
        audit: The acting principal and audit store the ``instrument.restore`` rows are written to.
        owner: The restoring caller's verified ``(sub, iss)`` pair. It owns any imported draft
            Policy; ``None`` when there is no verified caller.
        emitter: Log emitter for the server-side failure entry, or ``None`` for the process default.

    Returns:
        A :class:`RestorationAcceptedResponse` naming the canonical instrument id and the completed
        stages.

    Raises:
        CatalogSourceOverrideUnavailableError: The source override could not be read (503); nothing
            was fetched or restored.
        CuratedSourceUnavailableError: The source is unreachable, or the catalog listing or artifact
            is missing or malformed (502).
        RestoreInstrumentIdAmbiguousError: The id matches more than one catalog entry
            case-insensitively (409); nothing was fetched.
        RestoreInstrumentIdNotFoundError: The id matches no catalog entry (404); nothing was
            fetched.
        AuditTrailUnavailableError: The opening audit row could not be written; nothing was
            restored.
        RestoreArtifactRejectedError: The fetched artifact fails checksum or schema-version
            verification (422).
        GraphWriteUnavailableError: The artifact verified but the service has no Graph Write Gateway
            (503); nothing was written.
        RestoreStageFailedError: Any other failure, including an unset similarity-threshold setting
            (502). A gateway refusal, an unreachable store or a commit not yet applied is stage
            ``graph_write``.

    Side Effects:
        Reads the persisted source override, makes HTTP requests to the source, writes the opening
        and terminal ``instrument.restore`` audit rows and, on success, replaces the instrument's
        graphs through the Graph Write Gateway. Emits log entries.
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

    def _restore(audit_event_id: str) -> RestorationAcceptedResponse:
        return _perform_restore(
            instrument_id=canonical_id,
            config=config,
            actor=actor,
            dependencies=dependencies,
            audit_event_id=audit_event_id,
            invoke=lambda threshold, db, graph_name, open_gateway, event_id: dependencies.restore(
                artifact,
                db=db,
                single_tenant_graph_name=graph_name,
                similarity_threshold=threshold,
                actor=actor,
                open_gateway=open_gateway,
                audit_event_id=event_id,
                source=effective_source.url,
                owner=owner,
            ),
            emitter=emitter,
        )

    return _audited_restore(
        canonical_id, source="catalog", audit=audit, emitter=emitter, restore=_restore
    )


# --- default wiring (restore and company_merge imports below are function-local) ---


def _default_open_db(config: ServiceConfig) -> ReadOnlyDatabase:
    """Open the production graph database connection for ``config``."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off Company Merge at import
        connect_from_config,
    )

    return connect_from_config(config)


def _default_single_tenant_graph_name(config: ServiceConfig) -> str:
    """Return the single-tenant graph name; ``config`` is accepted but unused."""
    from ps_service.company_merge.falkordb_client import (  # noqa: PLC0415 -- function-local keeps ps_service.main off Company Merge at import
        single_tenant_graph_name,
    )

    _ = config
    return single_tenant_graph_name()


def build_default_restore_dependencies() -> RestoreDependencies:
    """Build the production :class:`RestoreDependencies`.

    The restore routine and Company Merge are imported inside the function, so importing the
    application module never loads them; they load on first use at request time.

    Returns:
        A :class:`RestoreDependencies` bound to the production restore routine, graph database
        connection, graph-name lookup and Graph Write Gateway access.
    """
    from ps_service.restore.restore_instrument import (  # noqa: PLC0415 -- function-local
        restore_instrument,
    )

    return RestoreDependencies(
        open_db=_default_open_db,
        single_tenant_graph_name=_default_single_tenant_graph_name,
        restore=restore_instrument,
        graph_write_access=graph_write_access,
    )


def _default_resolve_effective_source(config: ServiceConfig) -> EffectiveCatalogSource:
    """Resolve the effective curated-content source from the PS state database.

    Reads the source override from the runtime-config table; the graph database is not involved. A
    failed read raises ``RuntimeConfigError`` instead of falling back to the default source.
    """
    store = PsycopgRuntimeConfigStore(config, audit_store=PsycopgAuditStore(config))
    return resolve_effective_source(config, store=store)


def build_default_restore_from_catalog_infra() -> CatalogRestoreInfra:
    """Build the production external boundaries of the restore-from-catalog path.

    Returns the four boundaries a caller may substitute: the artifact fetch, the catalog fetch, the
    effective-source resolution and the graph database opener. It is its own module-level function
    so a test can replace exactly these boundaries by patching this name;
    :func:`build_default_restore_from_catalog_dependencies` looks the name up at call time and picks
    the replacement up. The restore routine itself is never part of this bundle.

    Returns:
        A :class:`CatalogRestoreInfra` bound to the production curated-content fetch, catalog fetch,
        effective-source resolution and graph database connection.
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
    """Build the production :class:`CatalogRestoreDependencies`.

    The restore routine is always the real ``restore_instrument``; only the external boundaries in
    ``infra`` can be substituted. The routine is imported inside the function, so importing the
    application module never loads it.

    Args:
        infra: The external boundaries to use, or ``None`` for the production ones from
            :func:`build_default_restore_from_catalog_infra`, resolved at call time.

    Returns:
        A :class:`CatalogRestoreDependencies` bound to the production restore routine and graph-name
        lookup, and to ``infra``'s boundaries.
    """
    from ps_service.restore.restore_instrument import (  # noqa: PLC0415 -- function-local
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
        graph_write_access=graph_write_access,
    )
