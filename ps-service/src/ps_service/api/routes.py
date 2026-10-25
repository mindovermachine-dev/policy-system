"""``APIRouter`` factory and route handlers for the PS Service REST API.

Each handler is thin wiring: it derives the caller from the request, delegates to an
orchestration function, and shapes the response model. Domain failures surface as API errors that
the application's error handlers map to HTTP statuses. ``build_api_router`` is a factory, so no
module-level router exists; the application includes the returned router once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool

from ps_service.api.change_check_orchestration import (
    ChangeCheckDependencies,
    ChangeCheckResult,
    run_change_check_sweep,
)
from ps_service.api.dependencies import (
    authenticated_only,
    get_principal,
    get_service_config,
    provide_audit_context,
    provide_change_check_dependencies,
    provide_curated_catalog_dependencies,
    provide_export_dependencies,
    provide_graph_write_gateway,
    provide_near_miss_review_dependencies,
    provide_pipeline_dependencies,
    provide_restore_dependencies,
    provide_restore_from_catalog_dependencies,
    provide_run_id,
    provide_system_audit_recorder,
    public_route,
    require_access_role,
)
from ps_service.api.errors import (
    CatalogSourceOverrideUnavailableError,
    CuratedSourceUnavailableError,
)
from ps_service.api.export_orchestration import ExportDependencies, run_export
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    run_audited_catalog_ingestion,
    run_internal_ingestion_pipeline,
)
from ps_service.api.models import (
    CatalogInstrumentEntry,
    CatalogRestorationRequest,
    ChangeCheckResponse,
    CuratedCatalogResponse,
    ExportAcceptedResponse,
    ExportRequest,
    IngestionAcceptedResponse,
    IngestionProgressResponse,
    IngestionRequest,
    InstrumentCheckOutcomeBody,
    PathIdentifier,
    PendingReviewListResponse,
    ResolveReviewRequest,
    ResolveReviewResponse,
    RestorationAcceptedResponse,
    RestorationRequest,
    ServiceStatusResponse,
    StageOutcome,
)
from ps_service.api.near_miss_review_orchestration import (
    NearMissReviewDependencies,
    run_list_near_misses,
    run_resolve_near_miss,
)
from ps_service.api.restore_orchestration import (
    CatalogRestoreDependencies,
    RestoreDependencies,
    run_restoration,
    run_restoration_from_catalog_source,
)
from ps_service.api.run_status import get_stage
from ps_service.api.service_status import build_service_status_handler
from ps_service.audit import (
    AuditContext,  # noqa: TC001 -- FastAPI resolves the route annotation at runtime
)
from ps_service.auth import (
    Principal,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.authz.models import AccessRole
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.curated_source.catalog_client import (
    CuratedCatalogDependencies,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.graph_gateway.system_audit import (
    SystemAuditRecorder,  # noqa: TC001 -- FastAPI resolves `create_ingestion`'s annotations at runtime
)
from ps_service.ingestion.graph_submission import (
    GroupSubmitter,  # noqa: TC001 -- FastAPI resolves `create_ingestion`'s annotations at runtime
)
from ps_service.runtime_config import RuntimeConfigError

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import IngestionOutcome


def _to_accepted_response(run_id: str, outcome: IngestionOutcome) -> IngestionAcceptedResponse:
    """Build the ``POST /ingestions`` success body from a finished ingestion.

    Args:
        run_id: The correlation id reported back to the caller.
        outcome: The ingestion result: instrument id, source, ``fresh`` or ``already_ingested``
            outcome, and the ordered stage reports.

    Returns:
        An :class:`IngestionAcceptedResponse` with one ``succeeded`` stage entry per stage report;
        ``stages`` is empty when the instrument was already ingested.
    """
    return IngestionAcceptedResponse(
        run_id=run_id,
        regulatory_instrument_id=outcome.regulatory_instrument_id,
        source=outcome.source,
        outcome=outcome.outcome,
        stages=[
            StageOutcome(stage=report.stage, status="succeeded", summary=report.summary)
            for report in outcome.stages
        ],
    )


async def create_ingestion(  # noqa: PLR0913, PLR0917 -- FastAPI dependency injection: one parameter per injected collaborator
    request_body: IngestionRequest,
    http_request: Request,
    run_id: Annotated[str, Depends(provide_run_id)],
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[PipelineDependencies, Depends(provide_pipeline_dependencies)],
    audit: Annotated[AuditContext, Depends(provide_audit_context)],
    gateway: Annotated[GroupSubmitter | None, Depends(provide_graph_write_gateway)],
    system_audit: Annotated[SystemAuditRecorder, Depends(provide_system_audit_recorder)],
    principal: Annotated[Principal | None, Depends(get_principal)] = None,
) -> IngestionAcceptedResponse:
    """Ingest one regulatory instrument and report the per-stage outcome (``POST /ingestions``).

    The request body selects the source. Both sources run synchronously and the call returns only
    when the run has finished. Pipeline work runs off the event loop.

    The caller must hold the ``ComplianceOfficer`` access role for either source; the route rejects
    any other caller, including a ``SystemAdmin`` or ``SystemOwner`` without an explicit grant, and
    a request with no verified caller, with 403 before this handler runs, so nothing is written
    and no stage starts.

    ``source: "catalog"`` ingests the CELEX named in the request. An ``ingestion_run.submit``
    audit row is written first; if it cannot be written nothing runs. The request's ``short_name``
    is rejected when a different CELEX in the graph already claims it. The CELEX is then resolved
    through Cellar/ELI under that ``short_name`` (used verbatim), and the document is fetched at
    most once per request. The Ingestion, Domain Mapper and Company Merge stages then run in order.
    A CELEX already present in the graph is rejected with 409. If the pipeline finds the instrument
    already fully merged it skips Domain Mapper and Company Merge and returns
    ``outcome="already_ingested"`` with an empty ``stages`` list.

    ``source: "internal"`` ingests an intake document carried in the request body. It runs one
    ``internal_ingestion`` stage that parses, validates and persists the
    document into the instrument's baseline and native graphs, and writes through the Graph Write
    Gateway. A document that carries a Policy needs a verified caller, because the imported Policy
    tree becomes a draft owned by that caller; the pipeline refuses an ownerless Policy-bearing
    document before any stage runs.

    Args:
        request_body: The request body, discriminated by ``source``: ``"catalog"`` with a CELEX,
            ``short_name`` and optional ``run_id``, or ``"internal"`` with the document content.
        http_request: The raw request; supplies the client host recorded as the caller.
        run_id: The server-minted run id for this request (injected). Used as the run's correlation
            id unless a catalog request supplies its own ``run_id``.
        config: The resolved service configuration (injected).
        dependencies: The pipeline collaborators: graph openers, stage runners and adapters
            (injected).
        audit: The acting principal and audit store for the ``ingestion_run`` rows (injected;
            catalog path only).
        gateway: The Graph Write Gateway, or ``None`` when the service has none (injected; internal
            path only).
        system_audit: Records the system audit row the internal path's graph groups are logged under
            (injected; internal path only).
        principal: The verified caller, or ``None`` when no verified identity exists, such as under
            the local-test bypass (injected). Owner of an imported Policy tree (internal path only).

    Returns:
        An :class:`IngestionAcceptedResponse` with the effective run id, the instrument id, the
        source, the outcome (``fresh`` or ``already_ingested``) and one entry per executed stage.
        ``stages`` is empty for ``already_ingested``.

    Raises:
        InternalIngestOwnerRequiredError: The internal document carries a Policy and the caller
            identity is missing; the role gate normally rejects such a caller first (401).
        AuditTrailUnavailableError: The opening audit row could not be written (503); nothing ran.
            Catalog path: the ``ingestion_run.submit`` row. Internal path: the system audit row.
        CatalogIdentifierNotFoundError: Catalog path only; the CELEX does not exist on Cellar/ELI
            (404).
        CelexAlreadyIngestedError: Catalog path only; the CELEX is already in the graph (409).
        ShortNameCollisionError: Catalog path only; the ``short_name`` is already claimed by a
            different CELEX (409).
        IngestionConfigIncompleteError: The pipeline configuration is incomplete (503).
        InternalSeedValidationError: Internal path only; the document fails structural or shape
            validation (422).
        GraphWriteUnavailableError: Internal path only; the service has no Graph Write Gateway
            (503); nothing ran.
        PipelineStageError: A pipeline stage, or the identity and pre-flight reads, failed (502).
            The error names the failing stage.

    Side Effects:
        Catalog path: writes an ``ingestion_run.submit`` audit row and a terminal
        ``ingestion_run.complete`` row, fetches the document from Cellar/ELI, and writes the
        ingested instrument to the graph. Internal path: writes the document into the instrument's
        graphs through the Graph Write Gateway, records a system audit row, and emits run log
        entries.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    if request_body.source == "internal":
        outcome = await run_in_threadpool(
            run_internal_ingestion_pipeline,
            request_body.content,
            config=config,
            run_id=run_id,
            caller=caller,
            dependencies=dependencies,
            gateway=gateway,
            system_audit=system_audit,
            owner=_restore_owner(principal),
        )
        return _to_accepted_response(run_id, outcome)
    effective_run_id = request_body.run_id or run_id
    outcome = await run_in_threadpool(
        run_audited_catalog_ingestion,
        request_body.celex,
        request_body.short_name,
        config=config,
        run_id=effective_run_id,
        caller=caller,
        dependencies=dependencies,
        audit=audit,
    )
    return _to_accepted_response(effective_run_id, outcome)


async def list_curated_catalog(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        CuratedCatalogDependencies, Depends(provide_curated_catalog_dependencies)
    ],
    principal: Annotated[Principal | None, Depends(get_principal)] = None,
) -> CuratedCatalogResponse:
    """List every curated instrument, external and internal (``GET /catalog``).

    The listing is not filtered by CELEX, so internal-source instruments appear too. It is fetched
    on every call from the effective curated-content source: the persisted source override when one
    exists, otherwise ``config.curated_source_base_url``. Both lookups are blocking and run off the
    event loop. The call needs no graph or LLM provider.

    Args:
        config: The resolved service configuration (injected); supplies the default curated-content
            source URL.
        dependencies: The curated-catalog collaborators that resolve the effective source and fetch
            its listing (injected).
        principal: The verified caller, or ``None`` when no verified identity exists (injected).
            Accepted but unused: the listing is the same for every caller.

    Returns:
        A :class:`CuratedCatalogResponse` with one entry per curated instrument (instrument id,
        title, source type, jurisdiction). An empty ``instruments`` list means the source lists
        none.

    Raises:
        CatalogSourceOverrideUnavailableError: The source override could not be read (503). The
            request is never served from the default source in its place.
        CuratedSourceUnavailableError: The effective source is unreachable, or its response is
            missing or malformed (502). Stale data is never served.

    Side Effects:
        Reads the persisted source override from the PS state database and makes one HTTP request to
        the curated-content source. Nothing is written.
    """
    del principal  # accepted for the dependency, not used: the listing is the same for all callers
    try:
        effective_source = await run_in_threadpool(dependencies.resolve_effective_source, config)
    except RuntimeConfigError as exc:
        raise CatalogSourceOverrideUnavailableError from exc
    try:
        entries = await run_in_threadpool(dependencies.fetch_catalog, effective_source.url)
    except CuratedSourceFetchError as exc:
        raise CuratedSourceUnavailableError(str(exc)) from exc
    return CuratedCatalogResponse(
        instruments=[
            CatalogInstrumentEntry(
                instrument_id=entry.instrument_id,
                title=entry.title,
                source_type=entry.source_type,
                jurisdiction=entry.jurisdiction,
            )
            for entry in entries
        ]
    )


def _restore_owner(principal: Principal | None) -> tuple[str, str] | None:
    """Return the restoring caller's ``(sub, iss)`` pair, or ``None`` with no verified identity.

    The pair owns any draft Policy that an internal restore or ingestion imports. There is no
    synthetic fallback on this surface: a request without a verified caller yields ``None``, and an
    artifact that carries Policy content is then refused rather than imported without an owner.
    """
    return (principal.sub, principal.iss) if principal is not None else None


async def create_restoration(
    request_body: RestorationRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[RestoreDependencies, Depends(provide_restore_dependencies)],
    audit: Annotated[AuditContext, Depends(provide_audit_context)],
    principal: Annotated[Principal | None, Depends(get_principal)] = None,
) -> RestorationAcceptedResponse:
    """Restore one curated instrument from an uploaded artifact (``POST /restorations``).

    The caller must hold the ``ComplianceOfficer`` access role; the route rejects any other caller,
    including a ``SystemAdmin`` or ``SystemOwner`` without an explicit grant, before this handler
    runs. The request carries the instrument manifest and the baseline and native graph blobs,
    base64-encoded; a blob that is not strict base64 is rejected with 422 before the handler runs.
    The handler decodes them, verifies the checksums and schema version, and
    restores the instrument into the graph. An artifact that carries Policy content is imported as a
    draft owned by the caller and is refused when there is no verified caller.

    Args:
        request_body: The instrument manifest and the base64-encoded baseline and native blobs.
        http_request: The raw request; supplies the client host recorded as the acting caller.
        config: The resolved service configuration (injected).
        dependencies: The restore collaborators: graph opener, graph-name lookup, restore routine
            and Graph Write Gateway access (injected).
        audit: The acting principal and audit store for the ``instrument.restore`` rows (injected).
        principal: The verified caller, or ``None`` when no verified identity exists (injected).
            Owner of any imported draft Policy.

    Returns:
        A :class:`RestorationAcceptedResponse` with the instrument id and one ``succeeded`` entry
        per completed restore stage.

    Raises:
        AuditTrailUnavailableError: The opening audit row could not be written (503); nothing was
            restored.
        RestoreArtifactRejectedError: The artifact fails checksum or schema-version verification
            (422).
        GraphWriteUnavailableError: The artifact verified but the service has no Graph Write Gateway
            (503); nothing was written.
        RestoreStageFailedError: Any other restore failure, including a missing Capability merge
            threshold setting (502). The error names the failing stage.

    Side Effects:
        Writes an ``instrument.restore`` opening audit row and a terminal row, and writes the
        restored instrument and its graphs through the Graph Write Gateway.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    return run_restoration(
        request_body,
        config=config,
        actor=caller,
        dependencies=dependencies,
        audit=audit,
        owner=_restore_owner(principal),
    )


async def create_restoration_from_catalog(
    request_body: CatalogRestorationRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        CatalogRestoreDependencies, Depends(provide_restore_from_catalog_dependencies)
    ],
    audit: Annotated[AuditContext, Depends(provide_audit_context)],
    principal: Annotated[Principal | None, Depends(get_principal)] = None,
) -> RestorationAcceptedResponse:
    """Fetch one curated instrument from the curated-content source and restore it.

    ``POST /restorations/from-catalog``. The caller must hold the ``ComplianceOfficer`` access role;
    the route rejects any other caller, including a ``SystemAdmin`` or ``SystemOwner`` without an
    explicit grant, before this handler runs. The requested instrument id is matched
    case-insensitively against the effective source's catalog listing, then the artifact is fetched
    and restored exactly as an uploaded artifact is: checksums and schema version are verified
    first. An artifact that carries Policy content is imported as a draft owned by the caller and is
    refused when there is no verified caller.

    Args:
        request_body: The curated instrument id to fetch and restore (any letter case).
        http_request: The raw request; supplies the client host recorded as the acting caller.
        config: The resolved service configuration (injected); supplies the default curated-content
            source URL.
        dependencies: The fetch-and-restore collaborators: catalog and artifact fetchers, source
            resolver, graph opener and restore routine (injected).
        audit: The acting principal and audit store for the ``instrument.restore`` rows (injected).
        principal: The verified caller, or ``None`` when no verified identity exists (injected).
            Owner of any imported draft Policy.

    Returns:
        A :class:`RestorationAcceptedResponse` with the canonical instrument id and one
        ``succeeded`` entry per completed restore stage.

    Raises:
        AuditTrailUnavailableError: The opening audit row could not be written (503); nothing was
            restored.
        CatalogSourceOverrideUnavailableError: The source override could not be read (503); nothing
            was fetched.
        CuratedSourceUnavailableError: The source is unreachable, or the catalog listing or artifact
            is missing or malformed (502).
        RestoreInstrumentIdAmbiguousError: The id matches more than one catalog entry
            case-insensitively (409).
        RestoreInstrumentIdNotFoundError: The id matches no catalog entry (404).
        RestoreArtifactRejectedError: The fetched artifact fails checksum or schema-version
            verification (422).
        GraphWriteUnavailableError: The artifact verified but the service has no Graph Write Gateway
            (503); nothing was written.
        RestoreStageFailedError: Any other restore failure, including a missing Capability merge
            threshold setting (502). The error names the failing stage.

    Side Effects:
        Makes HTTP requests to the curated-content source, reads the persisted source override,
        writes an ``instrument.restore`` opening audit row and a terminal row, and writes the
        restored instrument and its graphs through the Graph Write Gateway.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    return run_restoration_from_catalog_source(
        request_body,
        config=config,
        actor=caller,
        dependencies=dependencies,
        audit=audit,
        owner=_restore_owner(principal),
    )


async def create_export(
    request_body: ExportRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[ExportDependencies, Depends(provide_export_dependencies)],
) -> ExportAcceptedResponse:
    """Export one ingested curated instrument as a portable artifact (``POST /exports``).

    The caller must hold the ``ComplianceOfficer`` access role; the route rejects any other caller,
    including a ``SystemAdmin`` or ``SystemOwner`` without an explicit grant, before this handler
    runs. The export reads the instrument's baseline and native graphs, backfills missing embeddings
    through the Graph Write Gateway, and packages both graphs with a manifest, in the form ``POST
    /restorations`` accepts.

    Args:
        request_body: The instrument id to export.
        http_request: The raw request; supplies the client host recorded as the acting caller.
        config: The resolved service configuration (injected).
        dependencies: The export collaborators: graph reader and export routine (injected).

    Returns:
        An :class:`ExportAcceptedResponse` carrying the manifest and the base64-encoded baseline and
        native graph blobs.

    Raises:
        ExportInstrumentNotFoundError: The instrument id is malformed or names no ingested
            instrument (404).
        ExportConfigIncompleteError: The embedding model is not configured (503).
        ExportUnresolvedNearMissesError: Unresolved near-miss reviews touch the instrument's
            nodes (409); the message gives the count and the review ids, and nothing was written.
        ReviewKindUnsupportedError: A stored review has a kind with no registered spec (409,
            naming the kind); nothing was written.
        GraphWriteUnavailableError: The service has no Graph Write Gateway (503); nothing was opened
            or written.
        AuditTrailUnavailableError: The ``export.instrument`` audit row could not be written (503);
            nothing was computed or written.
        ExportStageFailedError: Any other export failure (502). The error names the failing stage.

    Side Effects:
        Writes an ``export.instrument`` audit row, writes embedding backfills through the Graph
        Write Gateway, emits ``started`` and terminal log entries, and uses a temporary scratch
        directory that is removed afterwards.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    return run_export(request_body, config=config, actor=caller, dependencies=dependencies)


def _to_change_check_response(result: ChangeCheckResult) -> ChangeCheckResponse:
    """Build the ``POST /change-checks`` success body from a finished sweep.

    Args:
        result: The sweep result: the run id and one outcome per tracked instrument.

    Returns:
        A :class:`ChangeCheckResponse` with the run id and one entry per tracked instrument, in the
        sweep's order.
    """
    return ChangeCheckResponse(
        run_id=result.run_id,
        instruments=[
            InstrumentCheckOutcomeBody(
                instrument_id=outcome.instrument_id,
                outcome=outcome.outcome,
                detail=outcome.detail,
                reingest_run_id=outcome.reingest_run_id,
            )
            for outcome in result.instruments
        ],
    )


async def create_change_check(
    run_id: Annotated[str, Depends(provide_run_id)],
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[ChangeCheckDependencies, Depends(provide_change_check_dependencies)],
    audit: Annotated[AuditContext, Depends(provide_audit_context)],
) -> ChangeCheckResponse:
    """Check every tracked instrument for amendments and re-ingest any that changed.

    ``POST /change-checks``. The caller must hold the ``ComplianceOfficer`` access role; the route
    rejects any other caller, including a ``SystemAdmin`` or ``SystemOwner`` without an explicit
    grant, before this handler runs. The sweep reads the set of tracked instruments once, polls the
    source for an amendment to each, and re-ingests those that changed. The sweep is blocking and
    runs off the event loop.

    Per-instrument outcomes are ``current`` (no amendment), ``poll_failed`` (the source could not be
    polled), ``not_configured`` (the instrument has no amendment-poll configuration),
    ``amendment_reingested``, ``reingest_failed`` and ``skipped`` (the instrument cannot be
    re-ingested, such as a national transposition). A failed re-ingest does not stop the sweep.

    Args:
        run_id: The server-minted run id for this request (injected); carried on every log entry of
            the sweep.
        config: The resolved service configuration (injected).
        dependencies: The change-check collaborators: graph openers, amendment poller and re-ingest
            routine (injected).
        audit: The acting principal and audit store (injected). Each re-ingest that runs is audited
            as an ``ingestion_run`` submit and complete pair attributed to this principal.

    Returns:
        A :class:`ChangeCheckResponse` with the run id and one entry per tracked instrument. An
        empty ``instruments`` list means no instrument is tracked.

    Raises:
        GraphWriteUnavailableError: The service has no Graph Write Gateway (503); nothing was opened
            or written.
        AuditTrailUnavailableError: An opening audit row for a re-ingest could not be written (503).
            The sweep stops before that instrument; earlier re-ingests stay recorded but their
            outcomes are not returned.

    Side Effects:
        Polls the amendment source, re-ingests changed instruments into the graph, writes
        ``ingestion_run`` audit rows, and emits sweep and per-instrument log entries.
    """
    result = await run_in_threadpool(
        run_change_check_sweep,
        config=config,
        run_id=run_id,
        dependencies=dependencies,
        audit=audit,
    )
    return _to_change_check_response(result)


async def list_near_misses(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        NearMissReviewDependencies, Depends(provide_near_miss_review_dependencies)
    ],
) -> PendingReviewListResponse:
    """List every unresolved near-miss review (``GET /near-misses``).

    A near-miss review is a pending Company Merge decision: two entities similar enough that a
    person must choose to keep them separate or merge them. The read is a fast, bounded graph query
    and runs directly on the event loop.

    Args:
        config: The resolved service configuration (injected).
        dependencies: The near-miss review collaborators: graph opener and review reader (injected).

    Returns:
        A :class:`PendingReviewListResponse` with one entry per unresolved review. An empty list
        means nothing awaits a decision.

    Raises:
        ReviewKindUnsupportedError: A stored review has a kind with no registered spec (409,
            naming the kind); the list is refused rather than hiding the review.

    Side Effects:
        Reads the graph. Nothing is written.
    """
    return run_list_near_misses(config=config, dependencies=dependencies)


async def resolve_near_miss(
    review_id: PathIdentifier,
    request_body: ResolveReviewRequest,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        NearMissReviewDependencies, Depends(provide_near_miss_review_dependencies)
    ],
    audit: Annotated[AuditContext, Depends(provide_audit_context)],
) -> ResolveReviewResponse:
    """Resolve one near-miss review by keeping the entities separate.

    ``POST /near-misses/{review_id}/resolve``. Deletes only the review record, writing an audit row
    first. Open to any authenticated caller. Merging near misses is done through the MCP
    ``merge-near-misses`` tool.

    Args:
        review_id: The review to resolve (path parameter): 1 to 64 letters, digits, underscores or
            hyphens. Any other value is rejected with 422 before the handler runs, so no graph
            read or audit row happens for it.
        request_body: The decision, ``"keep-separate"``.
        config: The resolved service configuration (injected).
        dependencies: The near-miss review collaborators: graph opener and review reader/resolver
            (injected).
        audit: The acting principal and audit store for the ``near_miss.resolve`` row (injected).

    Returns:
        A :class:`ResolveReviewResponse` naming the review and the decision.

    Raises:
        PendingReviewNotFoundError: The review does not exist or was already resolved, or one of
            its nodes no longer exists (404).
        ReviewKindUnsupportedError: The review has a kind with no registered spec (409, naming the
            kind). Nothing was audited or written.
        GraphWriteUnavailableError: The service has no Graph Write Gateway (503); nothing was
            audited or resolved.
        AuditTrailUnavailableError: The opening audit row could not be written (503); nothing was
            resolved.

    Side Effects:
        Writes a ``near_miss.resolve`` audit row and deletes the review record through the Graph
        Write Gateway.
    """
    return run_resolve_near_miss(
        review_id, request_body.decision, config=config, dependencies=dependencies, audit=audit
    )


async def get_ingestion_status(run_id: PathIdentifier) -> IngestionProgressResponse:
    """Report the pipeline stage currently executing for a run (``GET /ingestions/{run_id}``).

    A best-effort live-progress read, not a record lookup. It always answers 200, including for a
    run id that is unknown, already finished or not yet started; in those cases ``stage`` is empty.

    Args:
        run_id: The run id to look up (path parameter): 1 to 64 letters, digits, underscores or
            hyphens. Any other value is rejected with 422 before the handler runs.

    Returns:
        An :class:`IngestionProgressResponse` with ``run_id`` and the stage currently
        recorded for it, or ``None`` when no stage is recorded.
    """
    return IngestionProgressResponse(run_id=run_id, stage=get_stage(run_id))


def build_api_router() -> APIRouter:
    """Build the REST ``APIRouter`` that serves the PS Service API.

    A fresh router is returned on every call, so each application instance owns its own routes.
    Routes: ``GET /status``, ``GET /catalog``, ``POST /ingestions``, ``GET
    /ingestions/{run_id}``, ``POST /restorations``, ``POST /restorations/from-catalog``, ``POST
    /exports``, ``POST /change-checks``, ``GET /near-misses`` and ``POST
    /near-misses/{review_id}/resolve``. The
    ingestion (either source), restore, export
    and change-check routes require the ``ComplianceOfficer`` access role through a route-level
    dependency. ``GET /status`` is on ``RestAuthMiddleware``'s open list and is declared a public
    route: it needs no token, and its handler checks an optional bearer token itself.

    Returns:
        An ``APIRouter`` holding all of the routes above, ready for ``include_router``.
    """
    router = APIRouter()
    router.add_api_route(
        "/status",
        build_service_status_handler(),
        methods=["GET"],
        response_model=ServiceStatusResponse,
        response_model_exclude_none=True,
        dependencies=[
            Depends(
                public_route(
                    "the client status endpoint answers every caller; its handler gates the"
                    " detail on an optional verified token"
                )
            )
        ],
    )
    router.add_api_route(
        "/catalog",
        list_curated_catalog,
        methods=["GET"],
        dependencies=[
            Depends(
                authenticated_only(
                    "a read of the curated catalog listing; any authenticated caller"
                )
            )
        ],
    )
    router.add_api_route(
        "/ingestions",
        create_ingestion,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[
            Depends(
                require_access_role(AccessRole.COMPLIANCE_OFFICER, operation="POST /ingestions")
            )
        ],
    )
    router.add_api_route(
        "/ingestions/{run_id}",
        get_ingestion_status,
        methods=["GET"],
        dependencies=[
            Depends(authenticated_only("a best-effort progress read; any authenticated caller"))
        ],
    )
    router.add_api_route(
        "/restorations",
        create_restoration,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[
            Depends(
                require_access_role(AccessRole.COMPLIANCE_OFFICER, operation="POST /restorations")
            )
        ],
    )
    router.add_api_route(
        "/restorations/from-catalog",
        create_restoration_from_catalog,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[
            Depends(
                require_access_role(
                    AccessRole.COMPLIANCE_OFFICER, operation="POST /restorations/from-catalog"
                )
            )
        ],
    )
    router.add_api_route(
        "/exports",
        create_export,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[
            Depends(require_access_role(AccessRole.COMPLIANCE_OFFICER, operation="POST /exports"))
        ],
    )
    router.add_api_route(
        "/change-checks",
        create_change_check,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[
            Depends(
                require_access_role(AccessRole.COMPLIANCE_OFFICER, operation="POST /change-checks")
            )
        ],
    )
    router.add_api_route(
        "/near-misses",
        list_near_misses,
        methods=["GET"],
        dependencies=[
            Depends(authenticated_only("a plain read of open reviews; any authenticated caller"))
        ],
    )
    router.add_api_route(
        "/near-misses/{review_id}/resolve",
        resolve_near_miss,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[
            Depends(authenticated_only("keep-separate deletes only the review record; any caller"))
        ],
    )
    return router
