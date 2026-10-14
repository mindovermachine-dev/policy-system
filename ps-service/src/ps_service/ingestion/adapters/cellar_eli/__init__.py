"""The Cellar/ELI Ingestion Adapter package.

`CellarEliAdapter`, which composes the fetch, metadata and structure modules, is the package's
public entry point.
"""

from __future__ import annotations

from ps_service.ingestion.adapters.cellar_eli.adapter import CellarEliAdapter

__all__ = ["CellarEliAdapter"]
