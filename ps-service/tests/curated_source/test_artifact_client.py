"""Tests for ``ps_service.curated_source.artifact_client.fetch_artifact`` (AC-BI-004/006).

Mirrors ``test_catalog_client.py``'s own per-file fake-transport convention
(per-file duplication over a shared cross-file fake, `tests/restore/
test_restore_orchestration_content_rejection.py`'s documented rationale) --
here the fake transport scripts a *different* body per requested filename
(``manifest.json``/``baseline.json``/``native.json``), since one
``fetch_artifact`` call makes three requests.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn, Self

import pytest

from ps_service.curated_source.artifact_client import fetch_artifact
from ps_service.curated_source.errors import CuratedSourceFetchError

if TYPE_CHECKING:
    import urllib.request

    from api._fakes import MakeEmitter, ReadLines

_BASE_URL = "https://example.com/curated-content"
_INSTRUMENT_ID = "CRA-1.0"

_VALID_MANIFEST: dict[str, object] = {
    "instrument_id": "CRA-1.0",
    "celex": "32024R2847",
    "title": "Cyber Resilience Act",
    "short_name": "CRA",
    "version": "1.0",
    "source_type": "external",
    "jurisdiction": "EU",
    "schema_version": "1",
    "exported_at": "2026-01-01T00:00:00Z",
    "baseline_sha256": "a" * 64,
    "native_sha256": "b" * 64,
}

_BASELINE_BYTES = b'{"nodes": [], "edges": []}'
_NATIVE_BYTES = b'{"nodes": [], "edges": []}'


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
    """Scripts a different body per requested filename (last URL path segment)."""

    def __init__(self, bodies_by_filename: dict[str, bytes]) -> None:
        self._bodies_by_filename = bodies_by_filename
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> _FakeResponse:
        self.requests.append(request)
        filename = request.full_url.rsplit("/", 1)[-1]
        body = self._bodies_by_filename.get(filename)
        if body is None:
            message = f"unscripted request for {request.full_url!r}"
            raise AssertionError(message)
        return _FakeResponse(body)


class _FailingTransport:
    def __call__(self, request: urllib.request.Request, /, *, timeout: float) -> NoReturn:
        raise ConnectionRefusedError("connection refused")


def _valid_transport() -> _RecordingTransport:
    return _RecordingTransport(
        {
            "manifest.json": json.dumps(_VALID_MANIFEST).encode("utf-8"),
            "baseline.json": _BASELINE_BYTES,
            "native.json": _NATIVE_BYTES,
        }
    )


def test_fetch_artifact_requests_all_three_files_under_the_instrument_subdirectory(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    transport = _valid_transport()

    fetch_artifact(_BASE_URL, _INSTRUMENT_ID, transport=transport, emitter=emitter)

    requested_urls = {request.full_url for request in transport.requests}
    assert requested_urls == {
        f"{_BASE_URL}/{_INSTRUMENT_ID}/manifest.json",
        f"{_BASE_URL}/{_INSTRUMENT_ID}/baseline.json",
        f"{_BASE_URL}/{_INSTRUMENT_ID}/native.json",
    }


def test_fetch_artifact_returns_the_parsed_manifest_and_both_raw_blobs(
    make_emitter: MakeEmitter,
) -> None:
    emitter, _ = make_emitter()
    transport = _valid_transport()

    fetched = fetch_artifact(_BASE_URL, _INSTRUMENT_ID, transport=transport, emitter=emitter)

    assert fetched.manifest.instrument_id == "CRA-1.0"
    assert fetched.manifest.schema_version == "1"
    assert fetched.manifest.baseline_sha256 == "a" * 64
    assert fetched.baseline_blob == _BASELINE_BYTES
    assert fetched.native_blob == _NATIVE_BYTES


def test_fetch_artifact_raises_curated_source_fetch_error_naming_source_on_transport_failure(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006/004: an unreachable source names the source, no stale fallback."""
    emitter, _ = make_emitter()

    with pytest.raises(CuratedSourceFetchError) as excinfo:
        fetch_artifact(_BASE_URL, _INSTRUMENT_ID, transport=_FailingTransport(), emitter=emitter)

    assert _BASE_URL in str(excinfo.value)


def test_fetch_artifact_raises_curated_source_fetch_error_on_malformed_manifest(
    make_emitter: MakeEmitter,
) -> None:
    """AC-BI-006: a malformed manifest.json is refused before baseline/native are ever fetched."""
    emitter, _ = make_emitter()
    transport = _RecordingTransport(
        {
            "manifest.json": b"not json at all",
            "baseline.json": _BASELINE_BYTES,
            "native.json": _NATIVE_BYTES,
        }
    )

    with pytest.raises(CuratedSourceFetchError) as excinfo:
        fetch_artifact(_BASE_URL, _INSTRUMENT_ID, transport=transport, emitter=emitter)

    assert _BASE_URL in str(excinfo.value)
    # Only the manifest was ever requested -- fetch_artifact stops at the first failure.
    assert len(transport.requests) == 1


def test_fetch_artifact_emits_a_success_log_entry_naming_the_source_and_instrument(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    transport = _valid_transport()

    fetch_artifact(_BASE_URL, _INSTRUMENT_ID, transport=transport, emitter=emitter)
    emitter.flush(timeout=5.0)

    entries = read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["component"] == "curated_source"
    assert entries[0]["action"] == "fetch_artifact"
    assert entries[0]["outcome"] == "success"
    assert entries[0]["entity_id"] == _INSTRUMENT_ID
    assert entries[0]["source"] == _BASE_URL


def test_fetch_artifact_emits_a_failed_log_entry_naming_the_source_and_instrument_on_failure(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()

    with pytest.raises(CuratedSourceFetchError):
        fetch_artifact(_BASE_URL, _INSTRUMENT_ID, transport=_FailingTransport(), emitter=emitter)
    emitter.flush(timeout=5.0)

    entries = read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "failed"
    assert entries[0]["entity_id"] == _INSTRUMENT_ID
    assert entries[0]["source"] == _BASE_URL
