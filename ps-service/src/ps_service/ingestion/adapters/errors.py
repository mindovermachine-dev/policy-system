"""Domain-specific exception types for Ingestion Adapters (source-specific
fetch/parse failures) — distinct from `ps_service.ingestion.errors`, which
covers the Ingestion core pipeline's own persistence/configuration errors.
"""

from __future__ import annotations


class CellarFetchError(Exception):
    """The Cellar/ELI Adapter's HTTP fetch failed — the CELEX identifier
    doesn't resolve, or the Cellar/ELI service is unreachable/times out.

    Always raised via `raise CellarFetchError(...) from exc` so the
    original transport exception is preserved as `__cause__` — per the CA
    doc's "fails clearly and lets the caller decide whether to retry"
    (retry policy is deliberately not built into this component).
    """


class CellarParseError(Exception):
    """The fetched Cellar/ELI XHTML could not be parsed into structural
    nodes/edges or bibliographic metadata (malformed/unexpected markup).
    """
