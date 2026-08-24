"""ps_service.ingestion — package front door.

Re-exports `ingest_regulation`, the primary-use-case entry point
(`ps_service.ingestion.pipeline`), per PLAN_REVIEWED.md §2.1's file-layout
intent.
"""

from __future__ import annotations

from ps_service.ingestion.pipeline import ingest_regulation

__all__ = ["ingest_regulation"]
