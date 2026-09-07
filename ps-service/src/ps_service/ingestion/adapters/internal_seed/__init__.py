"""The internal-seed Ingestion Adapter package (issue #54).

Ingests a customer-authored, local-id JSON document (`docs/artifacts/
internal-regulation-intake-format.md`) rather than fetching from an external
source, unlike `cellar_eli`. S1 shipped the structural JSON Schema validation
layer (`schema.py`, D7); S2 adds the parse (`adapter.py`), referential-
integrity/cardinality checks and canonical-id minting, and dual-graph
persistence (`persist.py`) -- this module re-exports every public entry
point across both slices.
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
