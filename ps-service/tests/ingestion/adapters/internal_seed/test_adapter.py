"""Tests for `ps_service.ingestion.adapters.internal_seed.adapter.InternalSeedIngestionAdapter`.

S2's red-before-green tests 1/2 (PLAN.md S2): AC-BI-002 (unknown edge type
rejected) and AC-BI-003 (non-"internal" source_type rejected), both exercised
through the real `read_seed` composition (JSON Schema structural layer +
Pydantic parse), not just the schema layer alone (already covered by
`tests/ingestion/adapters/internal_seed/test_schema.py`).
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest

from ps_service.ingestion.adapters.internal_seed.adapter import InternalSeedIngestionAdapter
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError

if TYPE_CHECKING:
    from pathlib import Path

_VALID_SEED_DOCUMENT: dict[str, Any] = {
    "nodes": [
        {
            "label": "RegulatoryInstrument",
            "id": "ENGPRAC-3.0",
            "properties": {
                "title": "Engineering Practices Policy",
                "source_type": "internal",
                "effective_date": "2026-08-01",
                "version": "3.0",
                "status": "active",
            },
        },
        {
            "label": "Role",
            "id": "role-1",
            "properties": {"name": "Engineering Manager"},
        },
        {
            "label": "Obligation",
            "id": "obl-1",
            "properties": {"text": "Maintain approved policy governance"},
        },
    ],
    "edges": [
        {
            "type": "DEFINES",
            "from": {"label": "RegulatoryInstrument", "id": "ENGPRAC-3.0"},
            "to": {"label": "Role", "id": "role-1"},
            "properties": {"source_ref": "Sec. 1"},
        },
        {
            "type": "HAS",
            "from": {"label": "Role", "id": "role-1"},
            "to": {"label": "Obligation", "id": "obl-1"},
        },
    ],
}


def _write_seed(tmp_path: Path, document: dict[str, Any]) -> str:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def test_read_seed_accepts_a_well_formed_document(tmp_path: Path) -> None:
    """A schema-clean, well-typed document parses into an `InternalRegulationSeed`."""
    identifier = _write_seed(tmp_path, copy.deepcopy(_VALID_SEED_DOCUMENT))

    seed = InternalSeedIngestionAdapter().read_seed(identifier)

    assert len(seed.nodes) == 3
    assert len(seed.edges) == 2


def test_read_seed_rejects_unknown_edge_type(tmp_path: Path) -> None:
    """AC-BI-002: an edge `type` outside the five-member allow-list is rejected."""
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["edges"].append(
        {
            "type": "GOVERNED_BY",
            "from": {"label": "Obligation", "id": "obl-1"},
            "to": {"label": "Role", "id": "role-1"},
        }
    )
    identifier = _write_seed(tmp_path, document)

    with pytest.raises(InternalSeedError):
        InternalSeedIngestionAdapter().read_seed(identifier)


def test_read_seed_rejects_non_internal_source_type(tmp_path: Path) -> None:
    """AC-BI-003: a `RegulatoryInstrument.source_type` other than `"internal"` is rejected."""
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["nodes"][0]["properties"]["source_type"] = "external"
    identifier = _write_seed(tmp_path, document)

    with pytest.raises(InternalSeedError):
        InternalSeedIngestionAdapter().read_seed(identifier)


def test_read_seed_rejects_missing_file(tmp_path: Path) -> None:
    """A path that does not exist raises `InternalSeedError`, naming the path."""
    missing = str(tmp_path / "does-not-exist.json")

    with pytest.raises(InternalSeedError):
        InternalSeedIngestionAdapter().read_seed(missing)


def test_read_seed_rejects_invalid_json(tmp_path: Path) -> None:
    """A file that is not valid JSON raises `InternalSeedError`, not a bare `JSONDecodeError`."""
    path = tmp_path / "seed.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(InternalSeedError):
        InternalSeedIngestionAdapter().read_seed(str(path))
