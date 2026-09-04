"""Tests for `ps_service.api.catalog.load_regulation_catalog` (D12, CHANGES.md MA3).

Uses a `tmp_path`-based `catalog.json` fixture (two external entries, one
internal) rather than the packaged production copy, per PLAN.md Slice 6.5 --
this repo has no real, live-ingested `curated-content/catalog.json` with
genuine CRA/GDPR/NIS2 content yet.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ps_service.api.catalog import load_regulation_catalog

if TYPE_CHECKING:
    from pathlib import Path

_FIXTURE_ENTRIES = [
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
        "instrument_id": "GDPR-1.0",
        "celex": "32016R0679",
        "title": "General Data Protection Regulation",
        "source_type": "external",
        "jurisdiction": "EU",
        "short_name": "GDPR",
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


def _write_fixture(tmp_path: Path) -> Path:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(_FIXTURE_ENTRIES), encoding="utf-8")
    return catalog_path


def test_load_regulation_catalog_returns_every_entry_unfiltered(tmp_path: Path) -> None:
    """Slice 6.5: every entry -- external and internal -- comes back, all fields populated."""
    catalog_path = _write_fixture(tmp_path)

    entries = load_regulation_catalog(catalog_path)

    assert len(entries) == 3
    by_id = {entry.instrument_id: entry for entry in entries}
    assert by_id["CRA-1.0"].celex == "32024R2847"
    assert by_id["CRA-1.0"].source_type == "external"
    assert by_id["CRA-1.0"].jurisdiction == "EU"
    assert by_id["ENGPRAC-2.1"].celex is None
    assert by_id["ENGPRAC-2.1"].source_type == "internal"
    assert by_id["ENGPRAC-2.1"].jurisdiction is None
    assert by_id["ENGPRAC-2.1"].short_name == "ENGPRAC"
    assert by_id["ENGPRAC-2.1"].version == "2.1"


def test_load_regulation_catalog_preserves_file_order(tmp_path: Path) -> None:
    """No implicit re-sorting -- the reader trusts the writer's own ordering (D1)."""
    catalog_path = _write_fixture(tmp_path)

    entries = load_regulation_catalog(catalog_path)

    assert [entry.instrument_id for entry in entries] == [
        "CRA-1.0",
        "GDPR-1.0",
        "ENGPRAC-2.1",
    ]
