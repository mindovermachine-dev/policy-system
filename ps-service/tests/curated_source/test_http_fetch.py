"""Tests for ``ps_service.curated_source.http_fetch.fetch_bytes``.

Mirrors ``tests/ingestion/adapters/cellar_eli/test_fetch.py``'s own fake-
transport pattern exactly -- mocking at the transport boundary (L2 Testing
Patterns), never reaching real network.
"""

from __future__ import annotations

import email.message
import urllib.error
import urllib.request
from typing import NoReturn, Self

import pytest

from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.http_fetch import fetch_bytes


class _FakeResponse:
    """A minimal stand-in for what `urllib.request.urlopen` returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _RecordingTransport:
    """Captures the exact `Request`/`timeout` it was called with."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return _FakeResponse(self._body)


class _FailingTransport:
    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
        raise TimeoutError("connection timed out")


def test_fetch_bytes_sends_exact_url() -> None:
    transport = _RecordingTransport(b'[{"instrument_id": "CRA-1.0"}]')

    fetch_bytes("https://example.com/curated-content/catalog.json", transport=transport)

    assert len(transport.requests) == 1
    assert transport.requests[0].full_url == "https://example.com/curated-content/catalog.json"


def test_fetch_bytes_returns_the_response_body() -> None:
    transport = _RecordingTransport(b'[{"instrument_id": "CRA-1.0"}]')

    result = fetch_bytes("https://example.com/curated-content/catalog.json", transport=transport)

    assert result == b'[{"instrument_id": "CRA-1.0"}]'


def test_fetch_bytes_wraps_transport_failure_in_curated_source_fetch_error_preserving_cause() -> (
    None
):
    transport = _FailingTransport()

    with pytest.raises(CuratedSourceFetchError) as excinfo:
        fetch_bytes("https://example.com/curated-content/catalog.json", transport=transport)

    assert isinstance(excinfo.value.__cause__, TimeoutError)
    assert "https://example.com/curated-content/catalog.json" in str(excinfo.value)


def test_fetch_bytes_wraps_an_http_error_status_naming_the_url() -> None:
    """AC-BI-006: an unreachable/failing source names the source URL and failure."""

    class _HttpErrorTransport:
        def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", email.message.Message(), None
            )

    with pytest.raises(CuratedSourceFetchError) as excinfo:
        fetch_bytes(
            "https://example.com/curated-content/catalog.json", transport=_HttpErrorTransport()
        )

    assert "https://example.com/curated-content/catalog.json" in str(excinfo.value)
    assert "404" in str(excinfo.value)
