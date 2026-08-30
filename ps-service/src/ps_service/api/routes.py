"""``APIRouter`` factory for the PS Service REST API.

``build_api_router`` mirrors ``create_app`` being a factory (no module-level
singleton router). ``create_app`` includes the returned router via
``app.include_router``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.concurrency import run_in_threadpool

from ps_service.api.catalog import REGULATION_CATALOG, find_by_celex
from ps_service.api.dependencies import (
    get_service_config,
    provide_pipeline_dependencies,
    provide_run_id,
)
from ps_service.api.errors import (
    CatalogIdentifierNotFoundError,
    InternalIngestionNotImplementedError,
)
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    run_catalog_ingestion_pipeline,
)
from ps_service.api.models import (
    IngestionAcceptedResponse,
    IngestionRequest,
    RegulationCatalogEntry,
    RegulationCatalogResponse,
    StageOutcome,
)
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import IngestionOutcome

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
    ``run_in_threadpool``, and returns the per-stage outcome (AC-BI-002). An
    unknown CELEX 404s before any stage runs (AC-BI-006); a stage failure surfaces
    as a 502 naming the failing stage (AC-BI-008). A ``source: "internal"``
    request validates and then returns a structured 501 -- the internal pipeline
    is issue #54.

    Args:
        request_body: The ``source``-discriminated request body.
        http_request: The raw request, for the caller host (M4).
        run_id: The request-scoped run id (injected).
        config: The resolved service configuration (injected).
        dependencies: The pipeline dependency bundle (injected; overridden in tests).

    Returns:
        An :class:`IngestionAcceptedResponse` with the run id and per-stage outcomes.

    Raises:
        CatalogIdentifierNotFoundError: The catalog request named an unknown CELEX (404).
        InternalIngestionNotImplementedError: The request selected ``source: "internal"`` (501).
    """
    caller = http_request.client.host if http_request.client else "unknown"
    if request_body.source == "internal":
        raise InternalIngestionNotImplementedError(_INTERNAL_NOT_IMPLEMENTED_MESSAGE)
    entry = find_by_celex(request_body.celex)
    if entry is None:
        message = f"No curated regulation has CELEX {request_body.celex!r}."
        raise CatalogIdentifierNotFoundError(message)
    outcome = await run_in_threadpool(
        run_catalog_ingestion_pipeline,
        entry,
        config=config,
        run_id=run_id,
        caller=caller,
        dependencies=dependencies,
    )
    return _to_accepted_response(run_id, outcome)


def build_api_router() -> APIRouter:
    """Build the PS Service REST ``APIRouter``.

    Returns:
        An ``APIRouter`` exposing ``GET /regulations`` and ``POST /ingestions``.
    """
    router = APIRouter()
    router.add_api_route("/regulations", list_regulations, methods=["GET"])
    router.add_api_route(
        "/ingestions",
        create_ingestion,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
    )
    return router
