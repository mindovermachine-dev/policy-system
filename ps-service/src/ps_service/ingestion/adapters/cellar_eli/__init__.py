"""The Cellar/ELI Ingestion Adapter package.

`CellarEliAdapter` (composing `fetch.py`/`metadata.py`/`structure.py`) is
the package's public entry point.
"""

from __future__ import annotations

from ps_service.ingestion.adapters.cellar_eli.adapter import CellarEliAdapter

__all__ = ["CellarEliAdapter"]
