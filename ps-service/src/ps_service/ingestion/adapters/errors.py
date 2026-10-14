"""Exception types for Ingestion Adapters (source-specific fetch and parse failures).

Distinct from `ps_service.ingestion.errors`, which covers the ingestion core pipeline's own
persistence and configuration errors.
"""

from __future__ import annotations


class CellarFetchError(Exception):
    """The Cellar/ELI Adapter's HTTP fetch failed.

    The CELEX identifier does not resolve, or the Cellar/ELI service is unreachable or times
    out. Always raised with `from exc`, so the original transport exception is preserved as
    `__cause__`. The adapter does not retry; the caller decides whether to retry.
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

    Structural nodes and edges, or bibliographic metadata, could not be extracted because the
    markup is malformed or not in the expected shape. Retrying the same document will not help.
    """
