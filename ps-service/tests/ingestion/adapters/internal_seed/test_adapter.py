"""Tests for `ps_service.ingestion.adapters.internal_seed.adapter.InternalSeedIngestionAdapter`.

S2's red-before-green tests 1/2 (PLAN.md S2): AC-BI-002 (unknown edge type
rejected) and AC-BI-003 (non-"internal" source_type rejected), both exercised
through the real `parse_seed` composition (JSON Schema structural layer +
Pydantic parse), not just the schema layer alone (already covered by
`tests/ingestion/adapters/internal_seed/test_schema.py`).

Issue #91 retypes `read_seed(identifier: str)` to `parse_seed(document:
dict[str, object])` -- the adapter no longer does any file I/O, so these
tests pass a document dict directly rather than writing one to `tmp_path`.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from ps_service.ingestion.adapters.internal_seed.adapter import InternalSeedIngestionAdapter
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError

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


def test_parse_seed_accepts_a_well_formed_document() -> None:
    """A schema-clean, well-typed document parses into an `InternalRegulationSeed`."""
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)

    seed = InternalSeedIngestionAdapter().parse_seed(document)

    assert len(seed.nodes) == 3
    assert len(seed.edges) == 2


def test_parse_seed_rejects_unknown_edge_type() -> None:
    """AC-BI-002: an edge `type` outside the allow-list is rejected.

    `VERIFIED_BY` (a real edge type in `ps-domain-concepts.md`'s wider
    ontology, but not part of this intake format's allow-list -- it connects
    a RiskPath, which this format does not support) is used here since
    `GOVERNED_BY`/`SUPPORTED_BY`/`IMPLEMENTED_BY` (GH #76 Slices 1-3) are now
    all valid edge types.
    """
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["edges"].append(
        {
            "type": "VERIFIED_BY",
            "from": {"label": "Obligation", "id": "obl-1"},
            "to": {"label": "Role", "id": "role-1"},
        }
    )

    with pytest.raises(InternalSeedError):
        InternalSeedIngestionAdapter().parse_seed(document)


def test_parse_seed_rejects_non_internal_source_type() -> None:
    """AC-BI-003: a `RegulatoryInstrument.source_type` other than `"internal"` is rejected."""
    document = copy.deepcopy(_VALID_SEED_DOCUMENT)
    document["nodes"][0]["properties"]["source_type"] = "external"

    with pytest.raises(InternalSeedError):
        InternalSeedIngestionAdapter().parse_seed(document)
