"""Tests for `ps_service.export.catalog_writer.write_catalog_json`
(PLAN.md Slice 3.4, CHANGES.md MA3): the aggregate `catalog.json` listing,
written identically to both the repo-root copy and a packaged copy.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ps_service.export.catalog_writer import write_catalog_json
from ps_service.export.models import InstrumentManifest

if TYPE_CHECKING:
    from pathlib import Path


def _external_manifest() -> InstrumentManifest:
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


def _internal_manifest() -> InstrumentManifest:
    return InstrumentManifest(
        instrument_id="ENGPRAC-2.1",
        celex=None,
        title="Engineering Practices Standard",
        short_name="ENGPRAC",
        version="2.1",
        source_type="internal",
        jurisdiction=None,
        schema_version="1",
        exported_at="2026-09-04T00:00:00+00:00",
        baseline_sha256="c" * 64,
        native_sha256="d" * 64,
    )


def test_write_catalog_json_writes_both_destinations_with_identical_bytes(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    write_catalog_json(repo_root, packaged_copy_path, [_external_manifest()])

    repo_copy = (repo_root / "catalog.json").read_bytes()
    packaged_copy = packaged_copy_path.read_bytes()
    assert repo_copy == packaged_copy


def test_write_catalog_json_writes_one_entry_per_manifest_sorted_by_instrument_id(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    write_catalog_json(repo_root, packaged_copy_path, [_external_manifest(), _internal_manifest()])

    entries = json.loads((repo_root / "catalog.json").read_text(encoding="utf-8"))
    assert [entry["instrument_id"] for entry in entries] == ["CRA-1.0", "ENGPRAC-2.1"]


def test_write_catalog_json_entry_shape_for_external_and_internal_manifests(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"

    write_catalog_json(repo_root, packaged_copy_path, [_external_manifest(), _internal_manifest()])

    entries = json.loads((repo_root / "catalog.json").read_text(encoding="utf-8"))
    by_id = {entry["instrument_id"]: entry for entry in entries}
    assert by_id["CRA-1.0"] == {
        "instrument_id": "CRA-1.0",
        "celex": "32024R2847",
        "title": "Cyber Resilience Act",
        "source_type": "external",
        "jurisdiction": "EU",
        "short_name": "CRA",
        "version": "1.0",
    }
    assert by_id["ENGPRAC-2.1"] == {
        "instrument_id": "ENGPRAC-2.1",
        "celex": None,
        "title": "Engineering Practices Standard",
        "source_type": "internal",
        "jurisdiction": None,
        "short_name": "ENGPRAC",
        "version": "2.1",
    }


def test_write_catalog_json_rerun_replaces_the_file_wholesale_never_merges_stale_entries(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    packaged_copy_path = tmp_path / "packaged" / "catalog.json"
    write_catalog_json(repo_root, packaged_copy_path, [_external_manifest(), _internal_manifest()])

    write_catalog_json(repo_root, packaged_copy_path, [_internal_manifest()])

    entries = json.loads((repo_root / "catalog.json").read_text(encoding="utf-8"))
    assert [entry["instrument_id"] for entry in entries] == ["ENGPRAC-2.1"]
