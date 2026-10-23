"""Pure ASGI middleware that records one request-duration observation per HTTP response.

Labels carry the method (a fixed set, anything else is `_OTHER`), the route template (never a
raw path; `__unmatched__` when no route matches) and the integer status code, so the series
count is bounded by the routes the app defines.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from fastapi.routing import iter_route_contexts
from starlette.routing import Mount, compile_path

if TYPE_CHECKING:
    import re
    from collections.abc import Callable

    from opentelemetry.metrics import Histogram
    from starlette.routing import BaseRoute
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    type _Table = list[tuple[re.Pattern[str], str]]

UNMATCHED_ROUTE = "__unmatched__"
OTHER_METHOD = "_OTHER"
_KNOWN_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "CONNECT", "TRACE"}
)
_INTERNAL_ERROR = 500


class _RouteTemplates:
    """Resolve a request path to the template of the route that serves it.

    The table is built on first use from the router's effective routes (including routes of
    included routers, with their prefixes), so routes added after the middleware is created
    are still seen. Resolution is a first-match scan in route order, like Starlette's router.
    """

    def __init__(self, router: object) -> None:
        self._router = router
        self._table: _Table | None = None

    def resolve(self, path: str) -> str:
        """Return the template of the first route whose pattern matches `path`.

        Args:
            path: The request path, relative to the application root.

        Returns:
            The route template, or `UNMATCHED_ROUTE` when no route matches.
        """
        if self._table is None:
            self._table = self._build()
        for pattern, template in self._table:
            if pattern.match(path):
                return template
        return UNMATCHED_ROUTE

    def _build(self) -> _Table:
        routes: list[BaseRoute] = list(getattr(self._router, "routes", []))
        table: _Table = []
        for context in iter_route_contexts(routes):
            template = context.path
            if template is None:
                continue
            original = context.original_route
            pattern = (
                original.path_regex if isinstance(original, Mount) else compile_path(template)[0]
            )
            table.append((pattern, template))
        return table


class MetricsMiddleware:
    """Record `http.server.request.duration` for every HTTP request that gets a response.

    It does not buffer bodies. A request whose response never starts (the client went away, or
    the task was cancelled) records nothing, because it has no status; cancellation is re-raised.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        histogram: Histogram,
        router: object,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wrap `app`.

        Args:
            app: The ASGI application to wrap.
            histogram: The request-duration instrument, created from the catalog.
            router: The application's router, whose `routes` are used to resolve templates.
            clock: Monotonic clock in seconds; injectable for tests.
        """
        self._app = app
        self._histogram = histogram
        self._templates = _RouteTemplates(router)
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Call the wrapped app and record the request when a response was started.

        Args:
            scope: The ASGI scope; only `http` scopes are measured.
            receive: The ASGI receive callable.
            send: The ASGI send callable.

        Raises:
            asyncio.CancelledError: Re-raised after a cancelled request, with nothing recorded.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        snapshot = dict(scope)
        status: list[int] = []

        async def send_and_note_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(message["status"])
            await send(message)

        started = self._clock()
        try:
            await self._app(scope, receive, send_and_note_status)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not status:
                status.append(_INTERNAL_ERROR)
            raise
        finally:
            if status:
                self._record(snapshot, status[0], self._clock() - started)

    def _record(self, snapshot: Scope, status: int, elapsed: float) -> None:
        """Record one observation from the scope as it was before the app ran."""
        method = snapshot["method"]
        path = snapshot["path"]
        root_path = snapshot.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]
        self._histogram.record(
            max(elapsed, 0.0),
            {
                "http.request.method": method if method in _KNOWN_METHODS else OTHER_METHOD,
                "http.route": self._templates.resolve(path),
                "http.response.status_code": status,
            },
        )
