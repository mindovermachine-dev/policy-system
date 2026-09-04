"""FastAPI dependency providers for the PS Service REST API.

Conventions (L2 -> API Patterns):

* ``get_service_config`` returns the :class:`ServiceConfig` ``create_app`` stashed
  on ``app.state`` — no module global, no closure over ``create_app``.
* ``provide_run_id`` is an **async generator** dependency. It must be async so the
  ``bind_run_context`` binding is entered in the request task itself and stays
  visible both to the endpoint and to any ``run_in_threadpool`` work it
  dispatches (anyio copies the context forward). A *sync* generator dependency
  would be entered on a worker thread via ``contextmanager_in_threadpool``, so
  the ``contextvars`` binding would be set on a copied context and lost to the
  endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TC002 — FastAPI needs it at runtime (else a 422 body field)

from ps_service.api.ingestion_orchestration import (
    PipelineDependencies,
    build_default_pipeline_dependencies,
)
from ps_service.api.restore_orchestration import (
    RestoreDependencies,
    build_default_restore_dependencies,
)
from ps_service.logging import bind_run_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ps_service.config import ServiceConfig


def get_service_config(request: Request) -> ServiceConfig:
    """Return the ``ServiceConfig`` bound to the running app.

    Args:
        request: The incoming request (FastAPI injects it).

    Returns:
        The :class:`ServiceConfig` ``create_app`` stored on ``app.state.config``.
    """
    config: ServiceConfig = request.app.state.config
    return config


async def provide_run_id(request: Request) -> AsyncIterator[str]:
    """Bind one run id for the whole request and yield it.

    The id is also stashed on ``request.state.run_id`` so exception handlers can
    read it when shaping an error body (AC-BI-010).

    Args:
        request: The incoming request (FastAPI injects it).

    Yields:
        The bound run id.
    """
    with bind_run_context() as run_id:
        request.state.run_id = run_id
        yield run_id


def provide_pipeline_dependencies() -> PipelineDependencies:
    """Return the production ``PipelineDependencies`` for the ingestion pipeline.

    A plain provider (not a generator) so tests can swap it wholesale via
    ``app.dependency_overrides`` with a fake bundle. The real bundle wires the
    shipped stage entry points, graph openers, and adapter factories through
    ``build_default_pipeline_dependencies``, whose Domain Mapper / Company Merge
    imports are all function-local (M6 -- ``ps_service.main`` never transitively
    loads those components at module load).

    Returns:
        The production :class:`PipelineDependencies` bundle.
    """
    return build_default_pipeline_dependencies()


def provide_restore_dependencies() -> RestoreDependencies:
    """Return the production ``RestoreDependencies`` for the restore orchestration.

    Mirrors :func:`provide_pipeline_dependencies` exactly: a plain provider
    (not a generator) so tests can swap it wholesale via
    ``app.dependency_overrides`` with a fake bundle. The real bundle wires the
    shipped restore entry point and FalkorDB connection/graph-name helpers
    through ``build_default_restore_dependencies``, whose
    ``ps_service.restore``/``ps_service.company_merge`` imports are all
    function-local (M6 -- ``ps_service.main`` never transitively loads those
    components at module load).

    Returns:
        The production :class:`RestoreDependencies` bundle.
    """
    return build_default_restore_dependencies()
