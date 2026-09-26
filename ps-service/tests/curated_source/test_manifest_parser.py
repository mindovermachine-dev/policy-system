"""Tests for ``ps_service.curated_source.manifest_parser.parse_manifest_json`` (AC-BI-004/006)."""

from __future__ import annotations

import json

import pytest

from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.manifest_parser import parse_manifest_json

_URL = "https://example.com/curated-content/CRA-1.0/manifest.json"

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


def test_parse_manifest_json_returns_an_instrument_manifest_field_for_field() -> None:
    manifest = parse_manifest_json(json.dumps(_VALID_MANIFEST).encode("utf-8"), url=_URL)

    assert manifest.instrument_id == "CRA-1.0"
    assert manifest.celex == "32024R2847"
    assert manifest.title == "Cyber Resilience Act"
    assert manifest.short_name == "CRA"
    assert manifest.version == "1.0"
    assert manifest.source_type == "external"
    assert manifest.jurisdiction == "EU"
    assert manifest.schema_version == "1"
    assert manifest.exported_at == "2026-01-01T00:00:00Z"
    assert manifest.baseline_sha256 == "a" * 64
    assert manifest.native_sha256 == "b" * 64


def test_parse_manifest_json_accepts_internal_source_with_null_celex_and_jurisdiction() -> None:
    internal_manifest = dict(_VALID_MANIFEST)
    internal_manifest["source_type"] = "internal"
    internal_manifest["celex"] = None
    internal_manifest["jurisdiction"] = None

    manifest = parse_manifest_json(json.dumps(internal_manifest).encode("utf-8"), url=_URL)

    assert manifest.source_type == "internal"
    assert manifest.celex is None
    assert manifest.jurisdiction is None


def test_parse_manifest_json_raises_curated_source_fetch_error_on_non_json_body() -> None:
    with pytest.raises(CuratedSourceFetchError) as excinfo:
        parse_manifest_json(b"not json at all", url=_URL)

    assert _URL in str(excinfo.value)


def test_parse_manifest_json_raises_curated_source_fetch_error_when_body_is_not_an_object() -> None:
    with pytest.raises(CuratedSourceFetchError):
        parse_manifest_json(json.dumps(["not", "an", "object"]).encode("utf-8"), url=_URL)


def test_parse_manifest_json_raises_curated_source_fetch_error_when_a_field_is_missing() -> None:
    incomplete = dict(_VALID_MANIFEST)
    del incomplete["schema_version"]

    with pytest.raises(CuratedSourceFetchError) as excinfo:
        parse_manifest_json(json.dumps(incomplete).encode("utf-8"), url=_URL)

    assert _URL in str(excinfo.value)


def test_parse_manifest_json_raises_curated_source_fetch_error_for_unrecognized_source_type() -> (
    None
):
    bad = dict(_VALID_MANIFEST)
    bad["source_type"] = "not-a-real-source-type"

    with pytest.raises(CuratedSourceFetchError):
        parse_manifest_json(json.dumps(bad).encode("utf-8"), url=_URL)


def test_parse_manifest_json_raises_curated_source_fetch_error_for_wrong_field_type() -> None:
    bad = dict(_VALID_MANIFEST)
    bad["baseline_sha256"] = 12345

    with pytest.raises(CuratedSourceFetchError):
        parse_manifest_json(json.dumps(bad).encode("utf-8"), url=_URL)
