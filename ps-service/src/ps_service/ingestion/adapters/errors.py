"""Domain-specific exception types for Ingestion Adapters.

Source-specific fetch/parse failures — distinct from
`ps_service.ingestion.errors`, which covers the Ingestion core pipeline's
own persistence/configuration errors.
"""

from __future__ import annotations


class CellarFetchError(Exception):
    """The Cellar/ELI Adapter's HTTP fetch failed.

    The CELEX identifier doesn't resolve, or the Cellar/ELI service is
    unreachable/times out. Always raised via `raise CellarFetchError(...)
    from exc` so the
    original transport exception is preserved as `__cause__` — per the CA
    doc's "fails clearly and lets the caller decide whether to retry"
    (retry policy is deliberately not built into this component).
    """


class CellarNotFoundError(CellarFetchError):
    """The Cellar/ELI Adapter's HTTP fetch returned a genuine 404.

    The CELEX identifier does not name a document Cellar/ELI knows about --
    distinct from a transport failure or any other HTTP error status: the
    host responded, so this is not a Cellar/ELI outage. Raised only for
    ``urllib.error.HTTPError`` with ``code == 404``; every other failure
    still raises the plain :class:`CellarFetchError`. Never accompanied by
    ``mark_unhealthy(CELLAR_ELI)``.
    """


class CellarParseError(Exception):
    """The fetched Cellar/ELI XHTML could not be parsed.

    Structural nodes/edges or bibliographic metadata could not be extracted
    (malformed/unexpected markup).
    """
