"""Tests for `ps_service.export.catalog_writer.write_manifest`/`read_manifest`
(PLAN.md Slice 3.3): `manifest.json` round-trips field-for-field.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

from ps_service.export.catalog_writer import read_manifest, write_manifest
from ps_service.export.models import InstrumentManifest

if TYPE_CHECKING:
    from pathlib import Path


def _manifest() -> InstrumentManifest:
    return InstrumentManifest(
        instrument_id="CRA-1.0",
        celex="32024R2847",
        title="Cyber Resilience Act",
        short_name="CRA",
        version="1.0",
        source_type="external",
        jurisdiction="EU",
        schema_version="1",
        exported_at="2026-09-04T00:00:00+00:00",
        baseline_sha256="a" * 64,
        native_sha256="b" * 64,
    )


def test_write_manifest_creates_instrument_dir_and_manifest_json(tmp_path: Path) -> None:
    instrument_dir = tmp_path / "CRA-1.0"

    write_manifest(instrument_dir, _manifest())

    assert (instrument_dir / "manifest.json").exists()


def test_write_manifest_round_trips_to_an_equal_instrument_manifest(tmp_path: Path) -> None:
    instrument_dir = tmp_path / "CRA-1.0"
    manifest = _manifest()

    write_manifest(instrument_dir, manifest)
    restored = read_manifest(instrument_dir)

    assert restored == manifest


def test_write_manifest_round_trips_internal_source_with_no_celex(tmp_path: Path) -> None:
    instrument_dir = tmp_path / "ENGPRAC-2.1"
    manifest = dataclasses.replace(
        _manifest(),
        instrument_id="ENGPRAC-2.1",
        celex=None,
        source_type="internal",
        jurisdiction=None,
        short_name="ENGPRAC",
    )

    write_manifest(instrument_dir, manifest)
    restored = read_manifest(instrument_dir)

    assert restored == manifest
    assert restored.celex is None
    assert restored.jurisdiction is None


def test_write_manifest_overwrites_an_existing_manifest_wholesale(tmp_path: Path) -> None:
    instrument_dir = tmp_path / "CRA-1.0"
    write_manifest(instrument_dir, _manifest())

    updated = dataclasses.replace(_manifest(), version="2.0", baseline_sha256="c" * 64)
    write_manifest(instrument_dir, updated)

    restored = read_manifest(instrument_dir)
    assert restored == updated


def test_write_manifest_writes_valid_json_with_all_fields(tmp_path: Path) -> None:
    instrument_dir = tmp_path / "CRA-1.0"

    write_manifest(instrument_dir, _manifest())

    document = json.loads((instrument_dir / "manifest.json").read_text(encoding="utf-8"))
    assert document == {
        "instrument_id": "CRA-1.0",
        "celex": "32024R2847",
        "title": "Cyber Resilience Act",
        "short_name": "CRA",
        "version": "1.0",
        "source_type": "external",
        "jurisdiction": "EU",
        "schema_version": "1",
        "exported_at": "2026-09-04T00:00:00+00:00",
        "baseline_sha256": "a" * 64,
        "native_sha256": "b" * 64,
    }
