"""The `GET /metrics` route, served on the service port."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool

if TYPE_CHECKING:
    from ps_service.metrics.provider import MetricsHandle


def build_metrics_router() -> APIRouter:
    """Build the router that serves the Prometheus scrape endpoint.

    The route is anonymous by design and left out of the OpenAPI schema. It reads the
    `MetricsHandle` from `request.app.state.metrics`.

    Returns:
        A new `APIRouter` with `GET /metrics`.

    Side Effects:
        None at build time; each request collects the app's instruments.
    """
    router = APIRouter()

    async def get_metrics(request: Request) -> Response:
        """Return the current metrics in the Prometheus text exposition format.

        Collection runs on a worker thread because observable callbacks may do blocking I/O.

        Args:
            request: The incoming request, used to reach `app.state.metrics`.

        Returns:
            A 200 response with the scrape body.
        """
        handle = cast("MetricsHandle", request.app.state.metrics)
        body, content_type = await run_in_threadpool(handle.render)
        return Response(body, media_type=content_type)

    router.add_api_route("/metrics", get_metrics, methods=["GET"], include_in_schema=False)
    return router
