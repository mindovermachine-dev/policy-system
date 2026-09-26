"""Tests for ``ps_service.curated_source.catalog_client.fetch_catalog`` (AC-BI-003/006)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn, Self

import pytest

from ps_service.curated_source.catalog_client import fetch_catalog
from ps_service.curated_source.errors import CuratedSourceFetchError

if TYPE_CHECKING:
    import urllib.request

    from api._fakes import MakeEmitter, ReadLines

_BASE_URL = "https://example.com/curated-content"

_VALID_ENTRIES = [
    {
        "instrument_id": "CRA-1.0",
        "celex": "32024R2847",
        "title": "Cyber Resilience Act",
        "source_type": "external",
        "jurisdiction": "EU",
        "short_name": "CRA",
        "version": "1.0",
    },
    {
        "instrument_id": "ENGPRAC-2.1",
        "celex": None,
        "title": "Engineering Practices",
        "source_type": "internal",
        "jurisdiction": None,
        "short_name": "ENGPRAC",
        "version": "2.1",
    },
]


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _RecordingTransport:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FakeResponse:
        self.requests.append(request)
        return _FakeResponse(self._body)


class _FailingTransport:
    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
        raise ConnectionRefusedError("connection refused")


def test_fetch_catalog_requests_catalog_json_under_the_base_url(make_emitter: MakeEmitter) -> None:
    emitter, _ = make_emitter()
    transport = _RecordingTransport(json.dumps(_VALID_ENTRIES).encode("utf-8"))

    fetch_catalog(_BASE_URL, transport=transport, emitter=emitter)

    assert len(transport.requests) == 1
    assert transport.requests[0].full_url == f"{_BASE_URL}/catalog.json"


def test_fetch_catalog_returns_every_entry_unfiltered_in_file_order(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    transport = _RecordingTransport(json.dumps(_VALID_ENTRIES).encode("utf-8"))

    entries = fetch_catalog(_BASE_URL, transport=transport, emitter=emitter)

    assert [entry.instrument_id for entry in entries] == ["CRA-1.0", "ENGPRAC-2.1"]
    assert entries[0].celex == "32024R2847"
    assert entries[0].source_type == "external"
    assert entries[1].celex is None
    assert entries[1].source_type == "internal"
    assert entries[1].jurisdiction is None


def test_fetch_catalog_raises_curated_source_fetch_error_naming_source_on_transport_failure(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006: an unreachable source names the source and the failure, no stale fallback."""
    emitter, _ = make_emitter()

    with pytest.raises(CuratedSourceFetchError) as excinfo:
        fetch_catalog(_BASE_URL, transport=_FailingTransport(), emitter=emitter)

    assert _BASE_URL in str(excinfo.value)


def test_fetch_catalog_raises_curated_source_fetch_error_on_non_json_body(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006: a malformed (non-JSON) response body is refused, naming the source."""
    emitter, _ = make_emitter()
    transport = _RecordingTransport(b"not json at all")

    with pytest.raises(CuratedSourceFetchError) as excinfo:
        fetch_catalog(_BASE_URL, transport=transport, emitter=emitter)

    assert _BASE_URL in str(excinfo.value)


def test_fetch_catalog_raises_curated_source_fetch_error_when_body_is_not_a_json_array(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    transport = _RecordingTransport(json.dumps({"not": "a list"}).encode("utf-8"))

    with pytest.raises(CuratedSourceFetchError):
        fetch_catalog(_BASE_URL, transport=transport, emitter=emitter)


def test_fetch_catalog_raises_curated_source_fetch_error_when_an_entry_is_missing_a_field(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    incomplete_entry = dict(_VALID_ENTRIES[0])
    del incomplete_entry["title"]
    transport = _RecordingTransport(json.dumps([incomplete_entry]).encode("utf-8"))

    with pytest.raises(CuratedSourceFetchError):
        fetch_catalog(_BASE_URL, transport=transport, emitter=emitter)


def test_fetch_catalog_raises_curated_source_fetch_error_for_unrecognized_source_type(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    bad_entry = dict(_VALID_ENTRIES[0])
    bad_entry["source_type"] = "not-a-real-source-type"
    transport = _RecordingTransport(json.dumps([bad_entry]).encode("utf-8"))

    with pytest.raises(CuratedSourceFetchError):
        fetch_catalog(_BASE_URL, transport=transport, emitter=emitter)


def test_fetch_catalog_emits_a_success_log_entry_with_the_source_and_instrument_count(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    transport = _RecordingTransport(json.dumps(_VALID_ENTRIES).encode("utf-8"))

    fetch_catalog(_BASE_URL, transport=transport, emitter=emitter)
    emitter.flush(timeout=5.0)

    entries = read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["component"] == "curated_source"
    assert entries[0]["action"] == "fetch_catalog"
    assert entries[0]["outcome"] == "success"
    assert entries[0]["source"] == _BASE_URL
    assert entries[0]["instrument_count"] == len(_VALID_ENTRIES)


def test_fetch_catalog_emits_a_failed_log_entry_naming_the_source_on_failure(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    with pytest.raises(CuratedSourceFetchError):
        fetch_catalog(_BASE_URL, transport=_FailingTransport(), emitter=emitter)
    emitter.flush(timeout=5.0)

    entries = read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "failed"
    assert entries[0]["source"] == _BASE_URL
