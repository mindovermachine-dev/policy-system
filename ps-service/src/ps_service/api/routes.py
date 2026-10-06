"""``APIRouter`` factory for the PS Service REST API.

``build_api_router`` mirrors ``create_app`` being a factory (no module-level
singleton router). ``create_app`` includes the returned router via
``app.include_router``.
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
    get_principal,
    get_service_config,
    provide_change_check_dependencies,
    provide_curated_catalog_dependencies,
    provide_export_dependencies,
    provide_near_miss_review_dependencies,
    provide_pending_approval_store,
    provide_pipeline_dependencies,
    provide_restore_dependencies,
    provide_restore_from_catalog_dependencies,
    provide_run_id,
    require_access_role,
)
from ps_service.api.errors import (
    CatalogSourceOverrideUnavailableError,
    CuratedSourceUnavailableError,
    MergeApprovalRequiresAuthenticatedCallerError,
    PendingApprovalNotFoundError,
)
from ps_service.api.export_orchestration import ExportDependencies, run_export
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    resolve_ingestion_entry,
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
    PendingApprovalStatusResponse,
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
from ps_service.authz.models import AccessRole
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.curated_source.catalog_client import (
    CuratedCatalogDependencies,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.passkey_signing.service import check_pending_approval, create_merge_pending_approval
from ps_service.passkey_signing.store import (
    PendingApprovalStore,  # noqa: TC001 -- FastAPI resolves the endpoint annotation at runtime
)
from ps_service.passkey_signing.webauthn_rp import signable_link_host
from ps_service.runtime_config import RuntimeConfigError

if TYPE_CHECKING:
    from ps_service.api.ingestion_orchestration import IngestionOutcome


def _to_accepted_response(run_id: str, outcome: IngestionOutcome) -> IngestionAcceptedResponse:
    """Map an :class:`IngestionOutcome` to the ``POST /ingestions`` success body."""
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
    ``run_in_threadpool``, and returns the per-stage outcome (AC-BI-002) --
    unless the resolved identifier already has a fully-merged
    ``RegulatoryInstrument`` (issue #135), in which case Domain Mapper and
    Company Merge are skipped entirely and the response reports
    ``outcome="already_ingested"`` with an empty ``stages`` list. This
    ``source: "catalog"`` path is gated behind
    ``require_access_role(AccessRole.COMPLIANCE_OFFICER)`` (issue #145), checked
    inline -- off the event loop, via ``run_in_threadpool`` -- immediately after
    the ``source == "internal"`` branch has already returned, before any
    Cellar/catalog pipeline dispatch begins; an unprivileged or unauthenticated
    caller gets a 403 (``AccessDeniedError``) with no pipeline call made. The
    catalog path then calls ``resolve_ingestion_entry`` (issue #193) -- the same
    shared function the ``ingest_regulation`` MCP tool calls. It rejects a
    ``short_name`` already claimed by a different CELEX in the live graph, then
    resolves *every* CELEX -- curated or not, the catalog is never consulted --
    through Cellar/ELI under the request's own ``short_name``, used verbatim, before
    the pipeline runs. A CELEX Cellar does not know 404s, a resolved CELEX runs the
    pipeline, and the document is fetched at most once for the whole request
    (AC-BI-006). A stage failure -- including a Cellar/ELI outage during resolution --
    surfaces as a 502 naming the failing stage
    (AC-BI-007/008). A ``source: "internal"`` request carries the intake
    document's content directly in the body (issue #91 -- no server-side path
    resolution) and runs the internal-seed pipeline (issue #54, S2): today,
    one ``internal_ingestion`` stage that parses, validates, mints, and
    persists the submission into ``{short}_baseline``/``{short}_native``.
    The internal path stays entirely ungated (AC-BI-011) -- it is
    ``ps-cli ingest document``'s existing internal-authoring flow, out of
    scope for issue #145.

    Args:
        request_body: The ``source``-discriminated request body.
        http_request: The raw request, for the caller host (M4) and for the
            ``ComplianceOfficer`` gate on the catalog path (issue #145).
        run_id: The request-scoped, server-minted run id (injected); used as
            the effective correlation id only when the request body doesn't
            supply its own (catalog requests only -- see ``effective_run_id``).
        config: The resolved service configuration (injected).
        dependencies: The pipeline dependency bundle (injected; overridden in tests).

    Returns:
        An :class:`IngestionAcceptedResponse` with the run id and per-stage outcomes.

    Raises:
        AccessDeniedError: The caller (catalog path only) lacks
            ``ComplianceOfficer`` (403; issue #145).
        CatalogIdentifierNotFoundError: The CELEX does not exist on Cellar/ELI (404).
        ShortNameCollisionError: The request's ``short_name`` is already claimed by
            a different CELEX in the graph (409).
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
    await run_in_threadpool(require_access_role(AccessRole.COMPLIANCE_OFFICER), http_request)
    effective_run_id = request_body.run_id or run_id
    single_tenant_graph = await run_in_threadpool(dependencies.graphs.single_tenant, config)
    resolution = await run_in_threadpool(
        resolve_ingestion_entry,
        request_body.celex,
        request_body.short_name,
        single_tenant_graph=single_tenant_graph,
    )
    outcome = await run_in_threadpool(
        run_catalog_ingestion_pipeline,
        resolution.entry,
        config=config,
        run_id=effective_run_id,
        caller=caller,
        dependencies=dependencies,
        ingestion_adapter=resolution.adapter,
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
    curated-content source: a persisted override (issue #130: a ``runtime_config`` row in the
    PS state Postgres) when one exists, else ``config.curated_source_base_url``
    (default: the public Policy System GitHub repo, AC-BI-001; overridable with no code change,
    AC-BI-002), resolved on every call via the injected
    ``dependencies.resolve_effective_source`` (AC-BI-013) before fetching via
    ``dependencies.fetch_catalog`` (AC-BI-003) -- no longer read from the
    build-time-packaged ``catalog.json`` copy. Both calls are blocking and
    dispatched off the event loop via ``run_in_threadpool``, mirroring
    ``create_change_check``'s own async/blocking-call pattern. Depends on no
    FalkorDB/LLM fixture at all -- a ``TestClient`` call against an app with
    neither wired still succeeds (AC-BI-011's "no LLM provider configured").
    Fails closed when the override cannot be read (issue #130, AC-BI-010, D-FAILCLOSED,
    ``ps_service.curated_source.resolve.resolve_effective_source``): the request is not
    served from ``config.curated_source_base_url`` in its place.

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
        CatalogSourceOverrideUnavailableError: The override could not be read (AC-BI-010) --
            HTTP 503, never a silent fallback to the env-var/default source.
        CuratedSourceUnavailableError: The configured source is unreachable,
            or its response is missing/malformed (AC-BI-006) -- HTTP 502,
            never a silent fallback to stale data.
    """
    del principal  # unused on this representative route; see docstring above
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

    Issue #183: the owner of any draft Policy a restore imports. Only a verified
    principal is ever used -- there is no synthetic fallback on this surface, so an
    internal restore without a verified caller is refused rather than creating an
    ownerless draft.
    """
    return (principal.sub, principal.iss) if principal is not None else None


async def create_restoration(
    request_body: RestorationRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[RestoreDependencies, Depends(provide_restore_dependencies)],
    principal: Annotated[Principal | None, Depends(get_principal)] = None,
) -> RestorationAcceptedResponse:
    """Restore one curated instrument's artifact (D5, ``POST /restorations``).

    Issue #145: gated at the route level behind ``require_access_role(AccessRole.
    COMPLIANCE_OFFICER)`` (a caller lacking that role, including a ``SystemAdmin``/
    ``SystemOwner`` with no explicit grant, is denied before this function ever
    runs -- no change to this function's own body). Thin route wiring over
    ``restore_orchestration.run_restoration`` -- a
    checksum/schema_version rejection surfaces as 422
    (``RestoreArtifactRejectedError``), any other restore failure as 502
    naming the failing stage (``RestoreStageFailedError``).

    Args:
        request_body: The artifact plus its manifest (base64-encoded blobs).
        http_request: The raw request, for the caller host (mirrors
            ``create_ingestion``'s own ``caller`` derivation).
        config: The resolved service configuration (injected).
        dependencies: The restore dependency bundle (injected; overridden in tests).
        principal: The verified caller (injected); becomes the owner of any imported
            draft Policy (issue #183). ``None`` when no verified identity exists.

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    return run_restoration(
        request_body,
        config=config,
        actor=caller,
        dependencies=dependencies,
        owner=_restore_owner(principal),
    )


async def create_restoration_from_catalog(
    request_body: CatalogRestorationRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        CatalogRestoreDependencies, Depends(provide_restore_from_catalog_dependencies)
    ],
    principal: Annotated[Principal | None, Depends(get_principal)] = None,
) -> RestorationAcceptedResponse:
    """Fetch and restore one curated instrument's artifact from the curated-content source.

    Issue #125, ``POST /restorations/from-catalog`` -- an additive sibling to
    ``POST /restorations`` (D-NEW-ROUTE): the upload path
    (``create_restoration``) is entirely unaffected by this route. Issue #145:
    gated at the route level behind ``require_access_role(AccessRole.
    COMPLIANCE_OFFICER)`` (a caller lacking that role, including a ``SystemAdmin``/
    ``SystemOwner`` with no explicit grant, is denied before this function ever
    runs -- no change to this function's own body). Thin route
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
        principal: The verified caller (injected); becomes the owner of any imported
            draft Policy (issue #183). ``None`` when no verified identity exists.

    Returns:
        A :class:`RestorationAcceptedResponse` naming the completed stages.
    """
    caller = http_request.client.host if http_request.client else "unknown"
    return run_restoration_from_catalog_source(
        request_body,
        config=config,
        actor=caller,
        dependencies=dependencies,
        owner=_restore_owner(principal),
    )


async def create_export(
    request_body: ExportRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[ExportDependencies, Depends(provide_export_dependencies)],
) -> ExportAcceptedResponse:
    """Export one already-ingested curated instrument (issue #71, ``POST /exports``).

    Issue #145: gated at the route level behind ``require_access_role(AccessRole.
    COMPLIANCE_OFFICER)`` (a caller lacking that role, including a ``SystemAdmin``/
    ``SystemOwner`` with no explicit grant, is denied before this function ever
    runs -- no change to this function's own body). Thin route wiring over
    ``export_orchestration.run_export`` -- an unknown
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

    Issue #145: gated at the route level behind ``require_access_role(AccessRole.
    COMPLIANCE_OFFICER)`` (a caller lacking that role, including a ``SystemAdmin``/
    ``SystemOwner`` with no explicit grant, is denied before this function ever
    runs -- no change to this function's own body). Delegates to
    ``change_check_orchestration.run_change_check_sweep`` (D2's
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


def _approval_base_url(request: Request) -> str:
    """Derive `{scheme}://{host}` for a pending approval's link (CHANGES.md F2).

    Mirrors `ps_service.auth.middleware._resource_metadata_url`'s own
    "scheme + host off the live request, never hardcoded" pattern, so the
    link works correctly under a local dev bind, a `kind` NodePort, and a
    prod ClusterIP+Ingress alike. `signable_link_host` then rewrites a
    loopback IP literal to `localhost`, without which the link a local bind
    produces could not be signed at all (issue #196).
    """
    return f"{request.url.scheme}://{signable_link_host(request.url.netloc)}"


async def resolve_near_miss(
    review_id: str,
    request_body: ResolveReviewRequest,
    http_request: Request,
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    dependencies: Annotated[
        NearMissReviewDependencies, Depends(provide_near_miss_review_dependencies)
    ],
    store: Annotated[PendingApprovalStore, Depends(provide_pending_approval_store)],
    principal: Annotated[Principal | None, Depends(get_principal)],
) -> ResolveReviewResponse:
    """Resolve one `PendingReview` (issue #35, `POST /near-misses/{review_id}/resolve`).

    `decision="keep-separate"` (AC-BI-004) is unaffected by issue #131: it
    deletes only the `PendingReview` record, exactly as before -- thin route
    wiring over `near_miss_review_orchestration.run_resolve_near_miss`,
    unchanged. A `review_id` that doesn't exist or was already resolved
    raises `PendingReviewNotFoundError` (-> HTTP 404, AC-BI-008); no graph
    write happens on that path.

    `decision="merge"` (issue #131, CHANGES.md F1) no longer executes the
    merge synchronously: it requires a real, verified `principal` (fails
    closed with HTTP 401 -- `MergeApprovalRequiresAuthenticatedCallerError`
    -- before any Postgres or FalkorDB write, for an unauthenticated caller
    or one under the local-test bypass) and calls
    `passkey_signing.service.create_merge_pending_approval` -- the exact same
    function the MCP `near_misses_resolve` tool's own merge branch calls, so
    there is never a second, parallel gating mechanism (F1's own fix). The
    response carries `pending_approval_id`/`approval_url`/`expires_at`
    instead of `winner_id`/`loser_id`, which stay `None` until that approval
    is actually signed (a later slice).

    Args:
        review_id: The `PendingReview` id to resolve (path parameter).
        request_body: The resolve decision.
        http_request: The raw request, for the approval link's `{base_url}`
            (merge only).
        config: The resolved service configuration (injected).
        dependencies: The near-miss review dependency bundle (injected;
            overridden in tests).
        store: The pending-approval store (injected; overridden in tests).
        principal: The request's verified identity, or `None` under the
            local-test bypass (injected).

    Returns:
        A :class:`ResolveReviewResponse` naming the resolved review and
        decision.

    Raises:
        MergeApprovalRequiresAuthenticatedCallerError: `decision="merge"`
            with no real, verified `principal` (HTTP 401).
        PendingReviewNotFoundError: `review_id` doesn't exist or was
            already resolved (HTTP 404).
    """
    if request_body.decision == "keep-separate":
        return run_resolve_near_miss(
            review_id, request_body.decision, config=config, dependencies=dependencies
        )
    if principal is None:
        raise MergeApprovalRequiresAuthenticatedCallerError(
            "a signed passkey approval requires a real authenticated caller"
        )
    approval = create_merge_pending_approval(
        review_id=review_id,
        actor=(principal.sub, principal.iss),
        base_url=_approval_base_url(http_request),
        config=config,
        near_miss_dependencies=dependencies,
        store=store,
    )
    return ResolveReviewResponse(
        review_id=review_id,
        decision="merge",
        pending_approval_id=approval.pending_approval_id,
        approval_url=approval.approval_url,
        expires_at=approval.expires_at,
    )


async def check_merge_pending_approval(
    pending_approval_id: str,
    store: Annotated[PendingApprovalStore, Depends(provide_pending_approval_store)],
    principal: Annotated[Principal | None, Depends(get_principal)],
) -> PendingApprovalStatusResponse:
    """Resumable status check for a merge's pending approval (issue #131, CHANGES.md F1).

    Uses the identical store-backed lookup-plus-ownership-check logic the
    MCP `near_misses_check_approval` tool uses
    (`passkey_signing.service.check_pending_approval`) -- an unknown id and
    an id belonging to a different caller are never distinguished (both
    raise the same `PendingApprovalNotFoundError`, AC-BI-015's leak-nothing
    rule).

    Args:
        pending_approval_id: The pending approval id to look up (path parameter).
        store: The pending-approval store (injected; overridden in tests).
        principal: The request's verified identity, or `None` under the
            local-test bypass (injected).

    Returns:
        A :class:`PendingApprovalStatusResponse` for the caller's own
        pending approval.

    Raises:
        PendingApprovalNotFoundError: `pending_approval_id` is unknown, or
            belongs to a different caller (HTTP 404).
    """
    actor = (principal.sub, principal.iss) if principal is not None else None
    status = check_pending_approval(
        pending_approval_id=pending_approval_id, actor=actor, store=store
    )
    if status is None:
        raise PendingApprovalNotFoundError(f"no pending approval with id {pending_approval_id!r}")
    return PendingApprovalStatusResponse(
        pending_approval_id=status.pending_approval_id,
        status=status.status,
        review_id=status.review_id,
        decision=status.decision,
        winner_id=status.winner_id,
        loser_id=status.loser_id,
        error=status.error,
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
        dependencies=[Depends(require_access_role(AccessRole.COMPLIANCE_OFFICER))],
    )
    router.add_api_route(
        "/restorations/from-catalog",
        create_restoration_from_catalog,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(require_access_role(AccessRole.COMPLIANCE_OFFICER))],
    )
    router.add_api_route(
        "/exports",
        create_export,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(require_access_role(AccessRole.COMPLIANCE_OFFICER))],
    )
    router.add_api_route(
        "/change-checks",
        create_change_check,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(require_access_role(AccessRole.COMPLIANCE_OFFICER))],
    )
    router.add_api_route("/near-misses", list_near_misses, methods=["GET"])
    router.add_api_route(
        "/near-misses/{review_id}/resolve",
        resolve_near_miss,
        methods=["POST"],
        status_code=status.HTTP_200_OK,
    )
    router.add_api_route(
        "/near-misses/approvals/{pending_approval_id}",
        check_merge_pending_approval,
        methods=["GET"],
    )
    return router
