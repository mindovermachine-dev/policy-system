"""Tests for ps_service.ingestion.adapters.cellar_eli.fetch."""

from __future__ import annotations

import email.message
import urllib.error
import urllib.request
from typing import NoReturn, Self

import pytest

from ps_service.dependency_health import CELLAR_ELI, is_healthy
from ps_service.ingestion.adapters.cellar_eli.fetch import (
    check_connectivity,
    fetch_xhtml,
)
from ps_service.ingestion.adapters.errors import CellarFetchError


class _FakeResponse:
    """A minimal stand-in for what `urllib.request.urlopen` returns: a
    context manager whose `read()` yields the body bytes.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _RecordingTransport:
    """Captures the exact `Request`/`timeout` it was called with, so the
    test can assert URL/headers precisely — mocking at the transport
    boundary, per L2 Testing Patterns.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return _FakeResponse(self._body)


class _FailingTransport:
    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FakeResponse:
        raise TimeoutError("connection timed out")


def test_fetch_xhtml_sends_exact_url_and_headers_for_celex() -> None:
    transport = _RecordingTransport(b"<html></html>")

    fetch_xhtml("32024R2847", transport=transport)

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.full_url == "https://publications.europa.eu/resource/celex/32024R2847"
    assert request.get_header("Accept") == "application/xhtml+xml"
    assert request.get_header("Accept-language") == "eng"


def test_fetch_xhtml_returns_the_response_body() -> None:
    transport = _RecordingTransport(b"<html><body>ok</body></html>")

    result = fetch_xhtml("32024R2847", transport=transport)

    assert result == b"<html><body>ok</body></html>"


def test_fetch_xhtml_wraps_transport_failure_in_cellar_fetch_error_preserving_cause() -> None:
    transport = _FailingTransport()

    with pytest.raises(CellarFetchError) as exc_info:
        fetch_xhtml("32024R2847", transport=transport)

    assert isinstance(exc_info.value.__cause__, TimeoutError)
    assert "32024R2847" in str(exc_info.value)


def test_fetch_xhtml_marks_cellar_eli_healthy_on_success() -> None:
    transport = _RecordingTransport(b"<html></html>")

    fetch_xhtml("32024R2847", transport=transport)

    assert is_healthy(CELLAR_ELI) is True


def test_fetch_xhtml_marks_cellar_eli_unhealthy_on_failure() -> None:
    transport = _FailingTransport()

    with pytest.raises(CellarFetchError):
        fetch_xhtml("32024R2847", transport=transport)

    assert is_healthy(CELLAR_ELI) is False


def test_check_connectivity_marks_cellar_eli_healthy_on_success() -> None:
    transport = _RecordingTransport(b"<html></html>")

    check_connectivity(transport=transport)

    assert is_healthy(CELLAR_ELI) is True


def test_check_connectivity_marks_cellar_eli_unhealthy_on_transport_failure() -> None:
    transport = _FailingTransport()

    with pytest.raises(CellarFetchError):
        check_connectivity(transport=transport)

    assert is_healthy(CELLAR_ELI) is False


def test_check_connectivity_treats_http_error_status_as_reachable() -> None:
    """A 404 on the bare domain still means the host responded — the point
    of this probe is confirming network reachability, not that any
    particular resource exists there.
    """

    class _HttpErrorTransport:
        def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", email.message.Message(), None
            )

    check_connectivity(transport=_HttpErrorTransport())

    assert is_healthy(CELLAR_ELI) is True


@pytest.mark.cellar_live
def test_fetch_xhtml_live_fetch_of_cra_returns_eli_container_markup() -> None:
    """Real fetch of CRA (Regulation (EU) 2024/2847, CELEX 32024R2847) —
    no local file/fixture/PDF as source, per AC-001.
    """
    result = fetch_xhtml("32024R2847")

    assert len(result) > 0
    assert b"eli-container" in result
