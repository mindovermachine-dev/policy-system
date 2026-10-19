"""FastAPI dependency providers for the PS Service REST API.

Conventions:

* ``get_service_config`` returns the :class:`ServiceConfig` that ``create_app`` stored on
  ``app.state``; there is no module global and no closure over ``create_app``.
* ``provide_run_id`` is an **async generator** dependency. It must be async so the
  ``bind_run_context`` binding is entered in the request task itself and stays visible both to
  the endpoint and to any ``run_in_threadpool`` work it dispatches (anyio copies the context
  forward). A sync generator dependency would be entered on a worker thread, so the
  ``contextvars`` binding would be set on a copied context and lost to the endpoint.
* Bundle providers (``provide_*_dependencies``) are plain functions so tests can replace them
  through ``app.dependency_overrides``. The default bundles import their heavy components inside
  functions, so importing ``ps_service.main`` does not load those components.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import (
    Depends,
    Request,
)

from ps_service.api.change_check_orchestration import (
    ChangeCheckDependencies,
    build_default_change_check_dependencies,
)
from ps_service.api.errors import AccessDeniedError
from ps_service.api.export_orchestration import (
    ExportDependencies,
    build_default_export_dependencies,
)
from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    build_default_pipeline_dependencies,
)
from ps_service.api.near_miss_review_orchestration import (
    NearMissReviewDependencies,
    build_default_near_miss_review_dependencies,
)
from ps_service.api.restore_orchestration import (
    CatalogRestoreDependencies,
    RestoreDependencies,
    build_default_restore_dependencies,
    build_default_restore_from_catalog_dependencies,
)
from ps_service.audit import AuditContext, AuditStore, PsycopgAuditStore, resolve_audit_actor
from ps_service.auth import Principal
from ps_service.authz.service import require_role
from ps_service.authz.store import PsycopgAccessRoleStore
from ps_service.config import (
    ServiceConfig,  # noqa: TC001 -- FastAPI resolves `provide_pending_approval_store`'s own annotation at runtime
)
from ps_service.curated_source.catalog_client import (
    CuratedCatalogDependencies,
    build_default_curated_catalog_dependencies,
)
from ps_service.graph_gateway.system_audit import (
    SystemAuditRecorder,
    build_system_audit_recorder,
)
from ps_service.logging import bind_run_context
from ps_service.passkey_signing.store import PendingApprovalStore, PsycopgPendingApprovalStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from ps_service.authz.models import AccessRole
    from ps_service.graph_cleanup.dependencies import GraphCleanupDependencies
    from ps_service.ingestion.graph_submission import GroupSubmitter

# The fixed message `AccessDeniedError` always carries; it leaks no internal detail. It repeats
# `ps_service.authz.service`'s private message instead of importing it so that each surface's
# raise site stays self-contained.
_ACCESS_DENIED_MESSAGE = "You do not have the required access role for this action."


def get_service_config(request: Request) -> ServiceConfig:
    """Return the ``ServiceConfig`` bound to the running app.

    Args:
        request: The incoming request (FastAPI injects it).

    Returns:
        The :class:`ServiceConfig` that ``create_app`` stored on ``app.state.config``.

    Raises:
        AttributeError: ``app.state.config`` was never set, meaning the app was not built by
            ``create_app``.
    """
    config: ServiceConfig = request.app.state.config
    return config


def get_principal(request: Request) -> Principal | None:
    """Return the request's verified identity, or ``None`` under the local-test bypass.

    ``RestAuthMiddleware`` stores the verified ``Principal`` on ``request.scope["ps_principal"]``
    before FastAPI's dependency injection runs; a plain ASGI middleware writes into ``scope``
    because ``app.state`` is app-lifetime, not per-request, state. This dependency is only the
    read side of that contract. It never verifies a token itself.

    Args:
        request: The incoming request (FastAPI injects it).

    Returns:
        The verified :class:`Principal`, or ``None`` when no principal was stored: the
        local-test bypass is active (no token was checked), or the caller never passed through
        the middleware. A stored value that is not a :class:`Principal` is also treated as
        absent.
    """
    principal = request.scope.get("ps_principal")
    if principal is not None and not isinstance(principal, Principal):
        # Defensive only: the middleware never stores anything else under this key, and a caller
        # that never runs the middleware never sets it, so `.get(...)` already returns `None`.
        return None
    return principal


def enforce_access_role(
    principal: Principal | None, config: ServiceConfig, role: AccessRole
) -> None:
    """Require that a verified caller holds `role` (or a role above it).

    The one check behind both the route-level `require_access_role` dependency and a route that
    gates only one branch of its body (`POST /near-misses/{id}/resolve`, merge only). It calls the
    same `ps_service.authz.service.require_role` as the gated MCP tools.

    Args:
        principal: The verified caller, or `None` when no verified identity exists.
        config: The service configuration; selects the access-role store and its audit store.
        role: The minimum `AccessRole` the caller must hold.

    Raises:
        AccessDeniedError: There is no verified caller (fail closed: no identity to look a permanent
            role up for), or the caller does not hold `role` or a role above it.
        AuthorizationStoreUnavailableError: The authorization store cannot be reached.

    Side Effects:
        Queries the access-role store.
    """
    if principal is None:
        raise AccessDeniedError(_ACCESS_DENIED_MESSAGE)
    require_role(
        (principal.sub, principal.iss),
        minimum=role,
        store=PsycopgAccessRoleStore(config, audit_store=PsycopgAuditStore(config)),
    )


def require_access_role(role: AccessRole) -> Callable[[Request], None]:
    """Build a FastAPI dependency that enforces a minimum access role on the caller.

    It calls the same `ps_service.authz.service.require_role` as the gated MCP tools, so both
    surfaces share one authorization mechanism. The restore, export, change-check and
    ingestion REST routes use it as a route-level dependency; it is also a directly callable
    dependency factory, shaped like :func:`get_principal` and :func:`get_service_config`.

    Args:
        role: The minimum `AccessRole` a caller must hold. `SystemOwner` also satisfies a
            `SystemAdmin` requirement.

    Returns:
        A dependency callable `(request) -> None` that returns `None` when the caller passes.
        When called it can raise:

        * `AccessDeniedError`: no verified `Principal` is bound to the request (fail closed,
          because there is no identity to look up a permanent role for), or the principal does
          not hold `role` or a role above it.
        * `AuthorizationStoreUnavailableError`: the authorization store cannot be reached.
        * `AttributeError`: `app.state.config` was never set.

    Side Effects:
        The returned callable queries the access-role store when it runs. Building the
        callable does nothing.
    """

    def _require_access_role(request: Request) -> None:
        enforce_access_role(get_principal(request), get_service_config(request), role)

    return _require_access_role


async def provide_run_id(request: Request) -> AsyncIterator[str]:
    """Bind one run id for the whole request and yield it.

    The id is also stored on ``request.state.run_id`` so exception handlers can read it when
    shaping an error body.

    Args:
        request: The incoming request (FastAPI injects it).

    Yields:
        The bound run id.

    Side Effects:
        Binds the run id into the logging context for the duration of the request and sets
        ``request.state.run_id``; the binding is released when the request finishes.
    """
    with bind_run_context() as run_id:
        request.state.run_id = run_id
        yield run_id


def provide_pipeline_dependencies() -> PipelineDependencies:
    """Return the production ``PipelineDependencies`` for the ingestion pipeline.

    A plain provider, not a generator, so tests can replace it wholesale through
    ``app.dependency_overrides`` with a fake bundle. The real bundle wires the shipped stage
    entry points, graph openers and adapter factories. The Domain Mapper and Company Merge
    imports are made inside functions, so ``ps_service.main`` does not load those components at
    module load.

    Returns:
        The production :class:`PipelineDependencies` bundle.

    Side Effects:
        None; no connection is opened while building the bundle.
    """
    return build_default_pipeline_dependencies()


def provide_graph_cleanup_dependencies() -> GraphCleanupDependencies:
    """Return the production graph-cleanup collaborators for the near-miss approval status route.

    A plain provider so tests can replace it through `app.dependency_overrides`. The graph-cleanup
    import is function-local, so importing `ps_service.main` does not load that component.

    Returns:
        The production :class:`GraphCleanupDependencies` bundle.

    Side Effects:
        None; no connection is opened while building the bundle.
    """
    from ps_service.graph_cleanup.dependencies import (  # noqa: PLC0415 -- function-local keeps ps_service.main off graph_cleanup at import
        build_default_graph_cleanup_dependencies,
    )

    return build_default_graph_cleanup_dependencies()


def provide_graph_write_gateway(request: Request) -> GroupSubmitter | None:
    """Return the graph write gateway that ``create_app``'s lifespan built, or ``None``.

    The gateway exists only when the PS state Postgres (which holds the graph log) is configured
    and the gateway could be built at startup. ``None`` is returned as such, never replaced by a
    stand-in: a pipeline that must write a graph fails closed on it.

    Args:
        request: The incoming request (FastAPI injects it).

    Returns:
        The gateway, or ``None`` when none was built.
    """
    return getattr(request.app.state, "graph_write_gateway", None)


def provide_system_audit_recorder(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
) -> SystemAuditRecorder:
    """Return the recorder of system audit rows, bound to the PS state Postgres of ``config``.

    For a pipeline that is ungated and so has no principal to attribute an audit row to, such as
    the internal-seed ingestion route.

    Args:
        config: The resolved service configuration.

    Returns:
        A recorder that inserts one system-actor ``audit_events`` row and returns its id.

    Side Effects:
        None while building. The returned recorder opens a new connection and writes one audit
        row each time it is called.
    """
    return build_system_audit_recorder(config)


def provide_restore_dependencies() -> RestoreDependencies:
    """Return the production ``RestoreDependencies`` for the restore orchestration.

    A plain provider, not a generator, so tests can replace it wholesale through
    ``app.dependency_overrides`` with a fake bundle. The real bundle wires the shipped restore
    entry point and the FalkorDB connection and graph-name helpers. The ``ps_service.restore``
    and ``ps_service.company_merge`` imports are made inside functions, so ``ps_service.main``
    does not load those components at module load.

    Returns:
        The production :class:`RestoreDependencies` bundle.

    Side Effects:
        None; no connection is opened while building the bundle.
    """
    return build_default_restore_dependencies()


def provide_restore_from_catalog_dependencies() -> CatalogRestoreDependencies:
    """Return the production ``CatalogRestoreDependencies`` for ``POST /restorations/from-catalog``.

    A plain provider, not a generator, so tests can replace it wholesale through
    ``app.dependency_overrides`` with a fake bundle wired to a fake HTTP transport and a fake
    restore stage. The real bundle wires the shipped
    ``ps_service.curated_source.artifact_client.fetch_artifact`` entry point and the same
    restore entry point and FalkorDB helpers that :func:`provide_restore_dependencies` uses.

    Returns:
        The production :class:`CatalogRestoreDependencies` bundle.

    Side Effects:
        None; no network call or connection is made while building the bundle.
    """
    return build_default_restore_from_catalog_dependencies()


def provide_export_dependencies() -> ExportDependencies:
    """Return the production ``ExportDependencies`` for the export orchestration.

    A plain provider, not a generator, so tests can replace it wholesale through
    ``app.dependency_overrides`` with a fake bundle. The real bundle wires the shipped
    ``export_instrument`` entry point and the FalkorDB connection and graph-opener helpers. The
    ``ps_service.export`` imports are made inside functions, so ``ps_service.main`` does not
    load that component at module load.

    Returns:
        The production :class:`ExportDependencies` bundle.

    Side Effects:
        None; no connection is opened while building the bundle.
    """
    return build_default_export_dependencies()


def provide_near_miss_review_dependencies() -> NearMissReviewDependencies:
    """Return the production ``NearMissReviewDependencies`` for the near-miss review workflow.

    A plain provider, not a generator, so tests can replace it wholesale through
    ``app.dependency_overrides`` with a fake bundle. The real bundle wires the shipped
    ``pending_review`` functions and the single-tenant graph opener. The
    ``ps_service.company_merge`` imports are made inside functions, so ``ps_service.main`` does
    not load that component at module load.

    Returns:
        The production :class:`NearMissReviewDependencies` bundle.

    Side Effects:
        None; no connection is opened while building the bundle.
    """
    return build_default_near_miss_review_dependencies()


def provide_curated_catalog_dependencies() -> CuratedCatalogDependencies:
    """Return the production ``CuratedCatalogDependencies`` for ``GET /catalog``.

    A plain provider, not a generator, so tests can replace it wholesale through
    ``app.dependency_overrides`` with a fake bundle wired to a fake HTTP transport. The real
    bundle wires the shipped ``ps_service.curated_source.catalog_client.fetch_catalog`` entry
    point.

    Returns:
        The production :class:`CuratedCatalogDependencies` bundle.

    Side Effects:
        None; no network call is made while building the bundle.
    """
    return build_default_curated_catalog_dependencies()


def provide_pending_approval_store(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
) -> PendingApprovalStore:
    """Return the production `PendingApprovalStore` for the merge-approval gate.

    A plain provider, not a generator, overridable in tests through `app.dependency_overrides`
    with a fake store (`PendingApprovalStore` is a `Protocol`). Unlike the bundle providers, it
    takes `config` as a FastAPI-injected sub-dependency, because `PsycopgPendingApprovalStore`
    needs it at construction.

    Args:
        config: The resolved service configuration, injected by FastAPI.

    Returns:
        A `PsycopgPendingApprovalStore` bound to `config`. It opens no connection until used, so
        constructing one per request is cheap.
    """
    return PsycopgPendingApprovalStore(config)


def provide_change_check_dependencies() -> ChangeCheckDependencies:
    """Return the production ``ChangeCheckDependencies`` for the change-check sweep.

    A plain provider, not a generator, so tests can replace it wholesale through
    ``app.dependency_overrides`` with a fake bundle. The real bundle wires the shipped
    Regulatory Change Monitor entry points. The ``ps_service.change_monitor`` imports are made
    inside functions, so ``ps_service.main`` does not load that component at module load.

    Returns:
        The production :class:`ChangeCheckDependencies` bundle.

    Side Effects:
        None; no connection is opened while building the bundle.
    """
    return build_default_change_check_dependencies()


def provide_audit_store(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
) -> AuditStore:
    """Return the production `AuditStore` for audited REST operations.

    A plain provider, overridable in tests through `app.dependency_overrides` with an in-memory
    store (the same idiom as :func:`provide_pending_approval_store`).

    Args:
        config: The resolved service configuration, injected by FastAPI.

    Returns:
        A `PsycopgAuditStore` bound to `config`. It opens no connection until a row is written,
        so constructing one per request is cheap.
    """
    return PsycopgAuditStore(config)


def provide_audit_context(
    config: Annotated[ServiceConfig, Depends(get_service_config)],
    principal: Annotated[Principal | None, Depends(get_principal)],
    audit_store: Annotated[AuditStore, Depends(provide_audit_store)],
) -> AuditContext:
    """Return who is acting and where their audit rows go, for the request.

    The actor is the verified principal. Under the local-test bypass (no principal) it is the
    `system:local-test-bypass` sentinel. A request with neither is refused rather than silently
    attributed to the sentinel.

    Args:
        config: The resolved service configuration, injected by FastAPI.
        principal: The verified caller, or `None` when there is none, injected by FastAPI.
        audit_store: The store that receives the request's audit rows, injected by FastAPI.

    Returns:
        An `AuditContext` pairing the resolved `(subject, issuer)` actor with `audit_store`.

    Raises:
        AuditActorUnresolvedError: There is no principal and the local-test bypass is not
            active.
    """
    actor = (principal.sub, principal.iss) if principal is not None else None
    return AuditContext(
        resolve_audit_actor(actor, is_local_test_bypass_active=config.is_local_test_bypass_active),
        audit_store,
    )
