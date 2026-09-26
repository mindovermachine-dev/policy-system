"""Domain-specific exception types for the Curated Source component.

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1/L2 Error Handling). Mirrors the shape
of every other `ps_service` component's `errors.py` module (e.g.
`ps_service.restore.errors`, `ps_service.ingestion.adapters.errors`).
"""

from __future__ import annotations


class CuratedSourceConfigurationError(Exception):
    """The configured curated-content source URL is invalid.

    Raised by :func:`ps_service.curated_source.source_url.validate_source_url`
    for a disallowed scheme (e.g. ``file://``, AC-BI-008) or a plain
    ``http://`` URL with TLS not explicitly opted out of (AC-BI-010) -- the
    one shared validator both `ps_service.config.load_config()` (startup)
    and the runtime `set-catalog-source` MCP tool (Slice 3, AC-BI-012)
    validate through, so both fail the exact same way on the exact same
    input.
    """


class CuratedSourceFetchError(Exception):
    """Fetching from the configured curated-content source failed.

    Raised by :func:`ps_service.curated_source.http_fetch.fetch_bytes` (a
    network/HTTP failure) or :func:`ps_service.curated_source.catalog_client.
    fetch_catalog` (a malformed or structurally invalid response body).
    Always names the source URL and the specific failure (AC-BI-006) -- the
    caller never falls back to stale data on this error.
    """


class CuratedSourceOverridePersistenceError(Exception):
    """A write to the persisted curated-content source override failed (issue #125, Slice 3).

    Raised by :func:`ps_service.curated_source.store.set_override`/
    :func:`~ps_service.curated_source.store.reset_override` when the
    underlying FalkorDB write fails -- mirrors `ps_service.company_merge.
    pending_review`'s own `CompanyMergePersistenceError` shape (own copy, not
    a shared import, D-PERSISTENCE). Never raised by
    :func:`~ps_service.curated_source.store.get_override` (a plain read, no
    wrapper -- mirrors `pending_review.list_pending_reviews`'s own
    "reads are unwrapped" convention) nor by
    :func:`ps_service.curated_source.resolve.resolve_effective_source`,
    which treats this (like any other override-check failure) as
    "no override" rather than letting it propagate (D-FAILOPEN).
    """
