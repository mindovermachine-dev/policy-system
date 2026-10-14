"""Ingestion adapter for customer-authored internal regulations.

Ingests a customer-authored, local-id JSON document (`docs/artifacts/
internal-regulation-intake-format.md`) that the caller has already read, instead of fetching
from an external source as `cellar_eli` does. Validation runs in layers: structural JSON Schema
validation (`schema.py`), a typed parse (`adapter.py`, `models.py`), then referential-integrity
and cardinality checks, canonical-id minting and dual-graph persistence (`persist.py`).

This package re-exports the public entry points of all layers.
"""

from __future__ import annotations

from ps_service.ingestion.adapters.internal_seed.adapter import InternalSeedIngestionAdapter
from ps_service.ingestion.adapters.internal_seed.errors import InternalSeedError
from ps_service.ingestion.adapters.internal_seed.models import InternalRegulationSeed
from ps_service.ingestion.adapters.internal_seed.persist import (
    InternalIngestResult,
    ingest_internal_regulatory_instrument,
)
from ps_service.ingestion.adapters.internal_seed.schema import validate_seed_document

__all__ = [
    "InternalIngestResult",
    "InternalRegulationSeed",
    "InternalSeedError",
    "InternalSeedIngestionAdapter",
    "ingest_internal_regulatory_instrument",
    "validate_seed_document",
]
