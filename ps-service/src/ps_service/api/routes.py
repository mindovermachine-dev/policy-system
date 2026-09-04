"""``APIRouter`` factory for the PS Service REST API.

``build_api_router`` mirrors ``create_app`` being a factory (no module-level
singleton router). ``create_app`` includes the returned router via
``app.include_router``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool

from ps_service.api.catalog import CATALOG, REGULATION_CATALOG, find_by_celex
from ps_service.api.dependencies import (
    get_service_config,
    provide_pipeline_dependencies,
    provide_restore_dependencies,
    provide_run_id,
)
from ps_service.api.errors import InternalIngestionNotImplementedError
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    resolve_via_cellar,
    run_catalog_ingestion_pipeline,
)
from ps_service.api.models import (
    CatalogInstrumentEntry,
    CuratedCatalogResponse,
    IngestionAcceptedResponse,
    IngestionRequest,
    IngestionStatusResponse,
    RegulationCatalogEntry,
    RegulationCatalogResponse,
    RestorationAcceptedResponse,
    RestorationRequest,
    StageOutcome,
)
from ps_service.api.restore_orchestration import RestoreDependencies, run_restoration
from ps_service.api.run_status import get_stage
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import IngestionOutcome
    from ps_service.ingestion.adapters.base import IngestionAdapter

_INTERNAL_NOT_IMPLEMENTED_MESSAGE = (
    "Internal-document ingestion is not implemented in this walking-skeleton "
    "release; it is tracked in issue #54 (mindovermachine-dev/policy-system)."
)


async def list_regulations(
    run_id: Annotated[str, Depends(provide_run_id)],
) -> RegulationCatalogResponse:
    """Return the curated EU-regulation catalog (CELEX + title).

    Args:
        run_id: The request-scoped run id (injected by ``provide_run_id``).

    Returns:
        The catalog as a :class:`RegulationCatalogResponse`, carrying the run id.
    """
    return RegulationCatalogResponse(
        regulations=[
            RegulationCatalogEntry(celex=entry.celex, title=entry.title)
            for entry in REGULATION_CATALOG
        ],
        run_id=run_id,
    )


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
    stage (AC-BI-007/008). A ``source: "internal"`` request validates and then
    returns a structured 501 -- the internal pipeline is issue #54.

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
        InternalIngestionNotImplementedError: The request selected ``source: "internal"`` (501).
    """
    caller = http_request.client.host if http_request.client else "unknown"
    if request_body.source == "internal":
        raise InternalIngestionNotImplementedError(_INTERNAL_NOT_IMPLEMENTED_MESSAGE)
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


async def list_curated_catalog() -> CuratedCatalogResponse:
    """Return every curated instrument (external and internal), AC-BI-011.

    Unlike ``list_regulations``, this is **not** CELEX-filtered -- it reads
    the full ``catalog.json`` listing (D12's ``load_regulation_catalog()``
    result, unfiltered) so ``ps-cli catalog list`` sees internal-source
    instruments too. Depends on no FalkorDB/LLM fixture at all -- a
    ``TestClient`` call against an app with neither wired still succeeds
    (AC-BI-011's "no LLM provider configured").

    Returns:
        A :class:`CuratedCatalogResponse` listing every curated entry.
    """
    return CuratedCatalogResponse(
        instruments=[
            CatalogInstrumentEntry(
                instrument_id=entry.instrument_id,
                title=entry.title,
                source_type=entry.source_type,
                jurisdiction=entry.jurisdiction,
            )
            for entry in CATALOG
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
        An ``APIRouter`` exposing ``GET /regulations``, ``POST /ingestions``,
        and ``GET /ingestions/{run_id}``.
    """
    router = APIRouter()
    router.add_api_route("/regulations", list_regulations, methods=["GET"])
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
    return router
