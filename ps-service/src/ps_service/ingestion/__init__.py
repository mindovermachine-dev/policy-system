"""Ingestion package front door.

Re-exports `ingest_regulatory_instrument` (defined in `ps_service.ingestion.pipeline`), the
entry point that fetches one regulatory instrument through an adapter and persists its native
structural graph. Callers import it from here instead of reaching into the pipeline module.
"""

from __future__ import annotations

from ps_service.ingestion.pipeline import ingest_regulatory_instrument

__all__ = ["ingest_regulatory_instrument"]
