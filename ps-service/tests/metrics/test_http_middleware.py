"""ASGI-level tests for the request-duration middleware, against small Starlette apps."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute

from metrics._scrape import label_values, samples
from ps_service.metrics import build_metrics
from ps_service.metrics.http_middleware import MetricsMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from opentelemetry.metrics import Histogram
    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

    from ps_service.metrics import MetricsHandle

_DURATION = "http_server_request_duration_seconds"
_METHOD = "http_request_method"
_ROUTE = "http_route"
_STATUS = "http_response_status_code"


async def _item(request: Request) -> PlainTextResponse:
    return PlainTextResponse(request.path_params["item_id"])


async def _boom(_request: Request) -> PlainTextResponse:
    raise RuntimeError("secret detail")


async def _chunks(_request: Request) -> StreamingResponse:
    async def body() -> AsyncIterator[bytes]:
        for chunk in (b"a", b"b", b"c"):
            yield chunk

    return StreamingResponse(body())


async def _mounted(scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse("mounted")(scope, receive, send)


async def _socket(_websocket: object) -> None:
    return None


def _app() -> Starlette:
    return Starlette(
        routes=[
            Route("/items/{item_id}", _item),
            Route("/boom", _boom),
            Route("/chunks", _chunks),
            Mount("/mcp", app=_mounted),
            WebSocketRoute("/ws", _socket),
        ]
    )


def _histogram(handle: MetricsHandle) -> Histogram:
    from opentelemetry.metrics import Histogram as ApiHistogram

    instrument = handle.instruments["http.server.request.duration"]
    assert isinstance(instrument, ApiHistogram)
    return instrument


class _Clock:
    def __init__(self, *ticks: float) -> None:
        self._ticks: Iterator[float] = iter(ticks)

    def __call__(self) -> float:
        return next(self._ticks)


def _instrumented(
    clock: _Clock | None = None,
) -> tuple[Starlette, MetricsHandle]:
    handle = build_metrics()
    app = _app()
    extra = {} if clock is None else {"clock": clock}
    app.add_middleware(MetricsMiddleware, histogram=_histogram(handle), router=app.router, **extra)
    return app, handle


def _scrape(handle: MetricsHandle) -> bytes:
    return handle.render()[0]


def test_templated_route_is_recorded_with_template_method_and_integer_status() -> None:
    app, handle = _instrumented()

    TestClient(app).get("/items/123")

    series = samples(_scrape(handle), _DURATION + "_count")
    assert [(s.labels[_METHOD], s.labels[_ROUTE], s.labels[_STATUS], s.value) for s in series] == [
        ("GET", "/items/{item_id}", "200", 1.0)
    ]
    assert "123" not in _scrape(handle).decode()
    handle.shutdown()


def test_duration_is_measured_with_the_injected_monotonic_clock() -> None:
    app, handle = _instrumented(_Clock(10.0, 10.25))

    TestClient(app).get("/items/1")

    total = [s.value for s in samples(_scrape(handle), _DURATION + "_sum")]
    assert total == [0.25]
    handle.shutdown()


def test_unmatched_paths_share_one_fixed_route_value() -> None:
    app, handle = _instrumented()
    client = TestClient(app)

    for index in range(25):
        client.get(f"/nothing/{index}")

    assert label_values(_scrape(handle), _DURATION + "_count", _ROUTE) == {"__unmatched__"}
    assert label_values(_scrape(handle), _DURATION + "_count", _STATUS) == {"404"}
    handle.shutdown()


def test_mount_sub_paths_all_land_on_the_mount_prefix() -> None:
    app, handle = _instrumented()
    client = TestClient(app)

    for suffix in ("a", "b/c", "d/e/f"):
        client.get(f"/mcp/{suffix}")

    series = samples(_scrape(handle), _DURATION + "_count")
    assert {s.labels[_ROUTE] for s in series} == {"/mcp"}
    assert sum(s.value for s in series) == 3.0
    handle.shutdown()


@pytest.mark.parametrize("method", ["FOO", "BREW"])
def test_nonstandard_methods_are_recorded_as_other(method: str) -> None:
    app, handle = _instrumented()

    TestClient(app).request(method, "/items/1")

    assert label_values(_scrape(handle), _DURATION + "_count", _METHOD) == {"_OTHER"}
    handle.shutdown()


def test_a_raised_exception_is_recorded_as_500_and_still_propagates() -> None:
    app, handle = _instrumented()

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    assert label_values(_scrape(handle), _DURATION + "_count", _STATUS) == {"500"}
    assert "secret detail" not in _scrape(handle).decode()
    handle.shutdown()


async def _drive(app: Starlette, scope: Scope, receive: Receive) -> list[Message]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


class _OneRequest:
    """A `receive` that delivers the request once, then waits until the response is done."""

    def __init__(self) -> None:
        self._delivered = False

    async def __call__(self) -> Message:
        if not self._delivered:
            self._delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _http_scope(path: str) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1),
    }


def test_streamed_body_chunks_pass_through_unbuffered_and_unchanged() -> None:
    plain = _app()
    wrapped, handle = _instrumented()

    expected = asyncio.run(_drive(plain, _http_scope("/chunks"), _OneRequest()))
    actual = asyncio.run(_drive(wrapped, _http_scope("/chunks"), _OneRequest()))

    assert [m.get("body") for m in actual] == [m.get("body") for m in expected]
    assert [m["type"] for m in actual] == [m["type"] for m in expected]
    handle.shutdown()


def test_lifespan_and_websocket_scopes_are_passed_through_unrecorded() -> None:
    handle = build_metrics()
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    middleware = MetricsMiddleware(inner, histogram=_histogram(handle), router=_app().router)

    async def run() -> None:
        async def send(_message: Message) -> None:
            return None

        for kind in ("lifespan", "websocket"):
            await middleware({"type": kind, "path": "/ws"}, _OneRequest(), send)

    asyncio.run(run())

    assert seen == ["lifespan", "websocket"]
    assert samples(_scrape(handle), _DURATION) == []
    handle.shutdown()


def test_a_request_that_ends_before_any_response_starts_records_nothing() -> None:
    handle = build_metrics()

    async def silent(_scope: Scope, _receive: Receive, _send: Send) -> None:
        return None

    middleware = MetricsMiddleware(silent, histogram=_histogram(handle), router=_app().router)

    async def run() -> None:
        async def send(_message: Message) -> None:
            return None

        await middleware(_http_scope("/items/1"), _OneRequest(), send)

    asyncio.run(run())

    assert samples(_scrape(handle), _DURATION) == []
    handle.shutdown()


def test_cancellation_propagates_and_records_nothing() -> None:
    handle = build_metrics()

    async def cancelled(_scope: Scope, _receive: Receive, _send: Send) -> None:
        raise asyncio.CancelledError

    middleware = MetricsMiddleware(cancelled, histogram=_histogram(handle), router=_app().router)

    async def run() -> None:
        async def send(_message: Message) -> None:
            return None

        await middleware(_http_scope("/items/1"), _OneRequest(), send)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())

    assert samples(_scrape(handle), _DURATION) == []
    handle.shutdown()


def test_a_lowercase_standard_method_is_recorded_as_other() -> None:
    wrapped, handle = _instrumented()
    scope = _http_scope("/items/1")
    scope["method"] = "get"

    asyncio.run(_drive(wrapped, scope, _OneRequest()))

    assert label_values(_scrape(handle), _DURATION + "_count", _METHOD) == {"_OTHER"}
    handle.shutdown()
