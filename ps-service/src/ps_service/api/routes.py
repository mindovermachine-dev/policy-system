"""``APIRouter`` factory for the PS Service REST API.

``build_api_router`` mirrors ``create_app`` being a factory (no module-level
singleton router). ``create_app`` includes the returned router via
``app.include_router``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool

from ps_service.api.catalog import find_by_celex
from ps_service.api.change_check_orchestration import (
    ChangeCheckDependencies,
    ChangeCheckResult,
    run_change_check_sweep,
)
from ps_service.api.dependencies import (
    get_principal,
    get_service_config,
    provide_change_check_dependencies,
    provide_curated_catalog_dependencies,
    provide_export_dependencies,
    provide_near_miss_review_dependencies,
    provide_pipeline_dependencies,
    provide_restore_dependencies,
    provide_restore_from_catalog_dependencies,
    provide_run_id,
)
from ps_service.api.errors import CuratedSourceUnavailableError
from ps_service.api.export_orchestration import ExportDependencies, run_export
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    resolve_via_cellar,
    run_catalog_ingestion_pipeline,
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
    IngestionRequest,
    IngestionStatusResponse,
    InstrumentCheckOutcomeBody,
    PendingReviewListResponse,
    ResolveReviewRequest,
    ResolveReviewResponse,
    RestorationAcceptedResponse,
    RestorationRequest,
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
from ps_service.auth import (
    Principal,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.curated_source.catalog_client import (
    CuratedCatalogDependencies,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.curated_source.errors import CuratedSourceFetchError

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import IngestionOutcome
    from ps_service.ingestion.adapters.base import IngestionAdapter


def _to_accepted_response(run_id: str, outcome: IngestionOutcome) -> IngestionAcceptedResponse:
    """Map an :class:`IngestionOutcome` to the ``POST /ingestions`` success body."""
    return IngestionAcceptedResponse(
        run_id=run_id,
        regulatory_instrument_id=outcome.regulatory_instrument_id,
        source=outcome.source,
        stages=[
            StageOutcome(stage=report.stage, status="succeeded", summary=report.summary)
            for report in outcome.stages
        ],
    )


async def create_ingestion(
    request_body: IngestionRequest,
    http_request: Request,
    run_id: Annotated[str, Depends(provide_run_id)],
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[PipelineDependencies, Depends(provide_pipeline_dependencies)],
) -> IngestionAcceptedResponse:
    """Trigger the in-process ingestion pipeline for one ``POST /ingestions`` request.

    A ``source: "catalog"`` request runs the full external pipeline (Ingestion ->
    Domain Mapper -> Company Merge) for the named CELEX, off the event loop via
    ``run_in_threadpool``, and returns the per-stage outcome (AC-BI-002). A CELEX
    absent from the curated catalog falls back to a Cellar/ELI existence lookup
    (``resolve_via_cellar``, also off the event loop) before the pipeline runs --
    a genuine miss on both sources 404s (AC-BI-005/006), a resolved CELEX runs the
    same pipeline a curated one would (AC-BI-003/004), fetching the document at
    most once for the whole request (AC-BI-006). A stage failure -- including a
    Cellar/ELI outage during resolution -- surfaces as a 502 naming the failing
    stage (AC-BI-007/008). A ``source: "internal"`` request carries the intake
    document's content directly in the body (issue #91 -- no server-side path
    resolution) and runs the internal-seed pipeline (issue #54, S2): today,
    one ``internal_ingestion`` stage that parses, validates, mints, and
    persists the submission into ``{short}_baseline``/``{short}_native``.

    Args:
        request_body: The ``source``-discriminated request body.
        http_request: The raw request, for the caller host (M4).
        run_id: The request-scoped, server-minted run id (injected); used as
            the effective correlation id only when the request body doesn't
            supply its own (catalog requests only -- see ``effective_run_id``).
        config: The resolved service configuration (injected).
        dependencies: The pipeline dependency bundle (injected; overridden in tests).

    Returns:
        An :class:`IngestionAcceptedResponse` with the run id and per-stage outcomes.

    Raises:
        CatalogIdentifierNotFoundError: The CELEX is absent from the curated
            catalog and does not exist on Cellar/ELI either (404).
        InternalSeedValidationError: The internal request's document fails
            structural or shape validation (422).
        PipelineStageError: A pipeline stage raised (502).
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
        )
        return _to_accepted_response(run_id, outcome)
    effective_run_id = request_body.run_id or run_id
    entry = find_by_celex(request_body.celex)
    ingestion_adapter: IngestionAdapter | None = None
    if entry is None:
        resolution = await run_in_threadpool(resolve_via_cellar, request_body.celex)
        entry = resolution.entry
        ingestion_adapter = resolution.adapter
    outcome = await run_in_threadpool(
        run_catalog_ingestion_pipeline,
        entry,
        config=config,
        run_id=effective_run_id,
        caller=caller,
        dependencies=dependencies,
        ingestion_adapter=ingestion_adapter,
    )
    return _to_accepted_response(effective_run_id, outcome)


async def list_curated_catalog(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        CuratedCatalogDependencies, Depends(provide_curated_catalog_dependencies)
    ],
    principal: Annotated[Principal | None, Depends(get_principal)] = None,
) -> CuratedCatalogResponse:
    """Return every curated instrument (external and internal), AC-BI-011.

    This is **not** CELEX-filtered -- it reflects the full ``catalog.json``
    listing so ``ps-cli catalog list`` sees internal-source instruments too.
    Since issue #125, the listing is fetched at runtime from the *effective*
    curated-content source: a persisted FalkorDB override when one exists,
    else ``config.curated_source_base_url`` (default: the public Policy
    System GitHub repo, AC-BI-001; overridable with no code change,
    AC-BI-002), resolved on every call via the injected
    ``dependencies.resolve_effective_source`` (AC-BI-013) before fetching via
    ``dependencies.fetch_catalog`` (AC-BI-003) -- no longer read from the
    build-time-packaged ``catalog.json`` copy. Both calls are blocking and
    dispatched off the event loop via ``run_in_threadpool``, mirroring
    ``create_change_check``'s own async/blocking-call pattern. Depends on no
    FalkorDB/LLM fixture at all -- a ``TestClient`` call against an app with
    neither wired still succeeds (AC-BI-011's "no LLM provider configured"):
    a FalkorDB outage during the override check falls open to
    ``config.curated_source_base_url`` rather than failing the request
    (D-FAILOPEN, ``ps_service.curated_source.resolve.resolve_effective_source``).

    ``principal`` (issue #58, AC-BI-005) is the representative route this
    plan proves the ``get_principal`` dependency against end to end: the
    verified caller identity ``RestAuthMiddleware`` bound to this request, or
    ``None`` under the local-test bypass (#67). This endpoint has no
    identity-scoped behavior of its own, so the value is accepted but
    otherwise unused here -- the dependency is equally importable by any
    other route that does need it.

    Args:
        config: The resolved service configuration (injected) -- names the
            effective curated-content source URL to fetch from.
        dependencies: The curated-catalog dependency bundle (injected;
            overridden in tests with a fake HTTP transport).
        principal: The request's verified identity, injected by
            :func:`ps_service.api.dependencies.get_principal`.

    Returns:
        A :class:`CuratedCatalogResponse` listing every curated entry.

    Raises:
        CuratedSourceUnavailableError: The configured source is unreachable,
            or its response is missing/malformed (AC-BI-006) -- HTTP 502,
            never a silent fallback to stale data.
    """
    del principal  # unused on this representative route; see docstring above
    effective_source = await run_in_threadpool(dependencies.resolve_effective_source, config)
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


async def create_restoration(
    request_body: RestorationRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[RestoreDependencies, Depends(provide_restore_dependencies)],
) -> RestorationAcceptedResponse:
    """Restore one curated instrument's artifact (D5, ``POST /restorations``).

    Thin route wiring over ``restore_orchestration.run_restoration`` -- a
    checksum/schema_version rejection surfaces as 422
    (``RestoreArtifactRejectedError``), any other restore failure as 502
    naming the failing stage (``RestoreStageFailedError``).

    Args:
        request_body: The artifact plus its manifest (base64-encoded blobs).
        http_request: The raw request, for the caller host (mirrors
            ``create_ingestion``'s own ``caller`` derivation).
        config: The resolved service configuration (injected).
        dependencies: The restore dependency bundle (injected; overridden in tests).

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    return run_restoration(request_body, config=config, actor=caller, dependencies=dependencies)


async def create_restoration_from_catalog(
    request_body: CatalogRestorationRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        CatalogRestoreDependencies, Depends(provide_restore_from_catalog_dependencies)
    ],
) -> RestorationAcceptedResponse:
    """Fetch and restore one curated instrument's artifact from the curated-content source.

    Issue #125, ``POST /restorations/from-catalog`` -- an additive sibling to
    ``POST /restorations`` (D-NEW-ROUTE): the upload path
    (``create_restoration``) is entirely unaffected by this route. Thin route
    wiring over ``restore_orchestration.run_restoration_from_catalog_source``:
    an unreachable source or a missing/malformed fetched artifact surfaces as
    502 (``CuratedSourceUnavailableError``, AC-BI-004/006); a checksum/
    schema_version rejection surfaces as 422
    (``RestoreArtifactRejectedError``, AC-BI-007/009, mirrors #66); any other
    restore failure surfaces as 502 naming the failing stage
    (``RestoreStageFailedError``).

    Args:
        request_body: The curated instrument id to fetch and restore.
        http_request: The raw request, for the caller host (mirrors
            ``create_restoration``'s own ``caller`` derivation).
        config: The resolved service configuration (injected) -- names the
            effective curated-content source URL to fetch from.
        dependencies: The fetch-and-restore dependency bundle (injected;
            overridden in tests).

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    return run_restoration_from_catalog_source(
        request_body, config=config, actor=caller, dependencies=dependencies
    )


async def create_export(
    request_body: ExportRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[ExportDependencies, Depends(provide_export_dependencies)],
) -> ExportAcceptedResponse:
    """Export one already-ingested curated instrument (issue #71, ``POST /exports``).

    Thin route wiring over ``export_orchestration.run_export`` -- an unknown
    or malformed ``instrument_id`` surfaces as 404
    (``ExportInstrumentNotFoundError``), a missing embedding model config as
    503 (``ExportConfigIncompleteError``), and any other export failure as
    502 naming the failing stage (``ExportStageFailedError``).

    Args:
        request_body: The instrument id to export.
        http_request: The raw request, for the caller host (mirrors
            ``create_restoration``'s own ``caller`` derivation).
        config: The resolved service configuration (injected).
        dependencies: The export dependency bundle (injected; overridden in tests).

    Returns:
        An :class:`ExportAcceptedResponse` carrying the manifest and both
        base64-encoded graph blobs.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    return run_export(request_body, config=config, actor=caller, dependencies=dependencies)


def _to_change_check_response(result: ChangeCheckResult) -> ChangeCheckResponse:
    """Map a ``ChangeCheckResult`` to the ``POST /change-checks`` success body."""
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
) -> ChangeCheckResponse:
    """Sweep every tracked instrument for amendments and re-ingest any found.

    No auth dependency, matching ``POST /ingestions``'s posture (AC-BI-001).
    Delegates to ``change_check_orchestration.run_change_check_sweep`` (D2's
    algorithm, PLAN.md §4): opens the merged ``policy_system`` graph, reads
    the tracked-instrument set once, polls for amendments, and reports one
    outcome per tracked instrument -- ``current``/``poll_failed``/
    ``not_configured`` (Slice 2) and, for a detected amendment,
    ``amendment_reingested``/``reingest_failed`` (Slice 3, D2-D7's
    ``_reingest_one`` call contract). ``skipped`` (the national-transposition
    guard, D10) is still a structural gap until Slice 4 wires it.

    Args:
        run_id: The request-scoped run id (injected by ``provide_run_id``).
        config: The resolved service configuration (injected).
        dependencies: The change-check dependency bundle (injected;
            overridden in tests).

    Returns:
        A :class:`ChangeCheckResponse` with the run id and each tracked
        instrument's outcome.
    """
    result = await run_in_threadpool(
        run_change_check_sweep, config=config, run_id=run_id, dependencies=dependencies
    )
    return _to_change_check_response(result)


async def list_near_misses(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        NearMissReviewDependencies, Depends(provide_near_miss_review_dependencies)
    ],
) -> PendingReviewListResponse:
    """Return every unresolved `PendingReview` (issue #35, `GET /near-misses`, AC-BI-003).

    Thin route wiring over ``near_miss_review_orchestration.run_list_near_misses``
    -- opens the single-tenant graph and reads back every unresolved
    `PendingReview` node, mirroring ``create_restoration``'s "call the
    orchestration function directly (not ``run_in_threadpool``)" pattern:
    this is a fast, bounded Cypher read, not a multi-minute pipeline.

    Args:
        config: The resolved service configuration (injected).
        dependencies: The near-miss review dependency bundle (injected;
            overridden in tests).

    Returns:
        A :class:`PendingReviewListResponse` carrying one entry per
        unresolved `PendingReview` node.
    """
    return run_list_near_misses(config=config, dependencies=dependencies)


async def resolve_near_miss(
    review_id: str,
    request_body: ResolveReviewRequest,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        NearMissReviewDependencies, Depends(provide_near_miss_review_dependencies)
    ],
) -> ResolveReviewResponse:
    """Resolve one `PendingReview` (issue #35, `POST /near-misses/{review_id}/resolve`).

    Thin route wiring over `near_miss_review_orchestration.run_resolve_near_miss`
    -- `decision="keep-separate"` (AC-BI-004) deletes only the
    `PendingReview` record; `decision="merge"` (AC-BI-005/006/007) re-points
    every edge referencing the loser canonical node onto the
    deterministically-chosen winner, deletes the loser, and deletes the
    `PendingReview` record, atomically. A `review_id` that doesn't exist,
    was already resolved, or (merge only) references a node a prior merge
    already deleted, raises `PendingReviewNotFoundError` (-> HTTP 404,
    AC-BI-008); no graph write happens on that path. Mirrors
    `list_near_misses`'s "call the orchestration function directly, not
    `run_in_threadpool`" pattern -- a fast, bounded Cypher operation, not a
    multi-minute pipeline.

    Args:
        review_id: The `PendingReview` id to resolve (path parameter).
        request_body: The resolve decision.
        config: The resolved service configuration (injected).
        dependencies: The near-miss review dependency bundle (injected;
            overridden in tests).

    Returns:
        A :class:`ResolveReviewResponse` naming the resolved review and decision.
    """
    return run_resolve_near_miss(
        review_id, request_body.decision, config=config, dependencies=dependencies
    )


async def get_ingestion_status(run_id: str) -> IngestionStatusResponse:
    """Return ``run_id``'s currently-executing pipeline stage, best-effort.

    Always 200, including for an unknown, already-completed, or
    not-yet-started ``run_id`` (``stage: null``) -- a best-effort live-progress
    read over ``ps_service.api.run_status``, not authoritative resource
    retrieval (AC-BI-008, D4). ``IngestionAcceptedResponse`` is unaffected by
    this endpoint (AC-BI-011).

    Args:
        run_id: The run id to look up (path parameter).

    Returns:
        An :class:`IngestionStatusResponse` carrying ``run_id`` and the
        currently-recorded stage, or ``None`` if none is recorded.
    """
    return IngestionStatusResponse(run_id=run_id, stage=get_stage(run_id))


def build_api_router() -> APIRouter:
    """Build the PS Service REST ``APIRouter``.

    Returns:
        An ``APIRouter`` exposing ``GET /catalog``, ``POST /ingestions``,
        and ``GET /ingestions/{run_id}``.
    """
    router = APIRouter()
    router.add_api_route("/catalog", list_curated_catalog, methods=["GET"])
    router.add_api_route(
        "/ingestions",
        create_ingestion,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
    )
    router.add_api_route("/ingestions/{run_id}", get_ingestion_status, methods=["GET"])
    router.add_api_route(
        "/restorations",
        create_restoration,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
    )
    router.add_api_route(
        "/restorations/from-catalog",
        create_restoration_from_catalog,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
    )
    router.add_api_route(
        "/exports",
        create_export,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
    )
    router.add_api_route(
        "/change-checks",
        create_change_check,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
    )
    router.add_api_route("/near-misses", list_near_misses, methods=["GET"])
    router.add_api_route(
        "/near-misses/{review_id}/resolve",
        resolve_near_miss,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
    )
    return router
